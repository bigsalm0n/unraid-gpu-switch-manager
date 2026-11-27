<?php
declare(strict_types=1);

namespace Gsm;

const CONFIG = '/boot/config/plugins/gpu-switch-manager';
const RUN = '/run/gpu-switch-manager';
const PCI = '[0-9a-f]{4}:[0-9a-f]{2}:[01][0-9a-f]\.[0-7]';
const GPU_UUID = 'GPU-[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}';
const DOMAIN_UUID = '[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}';
const NAME = '[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}';
const VM = '[a-zA-Z0-9][a-zA-Z0-9 _.()-]{0,126}';

final class Refused extends \RuntimeException {}

function ensure(bool $condition, string $message): void
{
    if (!$condition) {
        throw new Refused($message);
    }
}

function matches(mixed $value, string $pattern): bool
{
    return is_string($value) && preg_match("~\\A(?:$pattern)\\z~D", $value) === 1;
}

function fields(mixed $record, string $names): void
{
    $expected = explode(' ', $names);
    ensure($record instanceof \stdClass && count((array)$record) === count($expected)
        && !array_diff($expected, array_keys((array)$record)), 'Unexpected or missing fields: ' . $names);
}

function ordered(array $items): array
{
    $pending = array_column($items, null, 'name');
    $result = [];
    while ($pending) {
        $ready = array_filter($pending, fn($item) => !array_intersect($item->after, array_keys($pending)));
        ensure((bool)$ready, 'Container dependencies form a cycle');
        foreach ($ready as $name => $item) {
            $result[] = $item;
            unset($pending[$name]);
        }
    }
    return $result;
}

function defaults(): \stdClass
{
    return (object)['version' => 2, 'enabled' => false, 'gpus' => (object)[], 'profiles' => (object)[]];
}

function validate(mixed $cfg): \stdClass
{
    fields($cfg, 'version enabled gpus profiles');
    ensure($cfg->version === 2 && is_bool($cfg->enabled), 'Expected configuration version 2 and boolean enabled');
    ensure($cfg->gpus instanceof \stdClass && $cfg->profiles instanceof \stdClass, 'Invalid configuration maps');
    $slots = $uuids = $assignments = $stoppable = [];
    foreach ($cfg->gpus as $key => $gpu) {
        fields($gpu, 'pci uuid functions protected');
        ensure(matches($key, NAME) && matches($gpu->pci, PCI) && is_bool($gpu->protected), 'Invalid GPU record');
        $slot = substr($gpu->pci, 0, -1);
        ensure(!isset($slots[$slot]), 'Duplicate physical slot');
        $slots[$slot] = true;
        if (!$gpu->protected || $gpu->uuid !== null) {
            ensure(matches($gpu->uuid, GPU_UUID) && !isset($uuids[strtolower($gpu->uuid)]), 'Missing or duplicate NVIDIA identity');
            $uuids[strtolower($gpu->uuid)] = true;
        }
        $number = substr($gpu->pci, -1);
        ensure($gpu->functions instanceof \stdClass && isset($gpu->functions->$number), 'Missing graphics fingerprint');
        foreach ($gpu->functions as $function => $signature) {
            ensure(matches($function, '[0-7]') && matches($signature, '[0-9a-f]{6}(?::[0-9a-f]{4}){4}'), 'Invalid PCI fingerprint');
        }
        ensure(str_starts_with($gpu->functions->$number, '03'), 'Expected display controller');
    }
    foreach ($cfg->profiles as $vm => $profile) {
        fields($profile, property_exists($profile, 'automatic') ? 'gpu containers automatic' : 'gpu containers');
        ensure(!property_exists($profile, 'automatic') || is_bool($profile->automatic), 'Invalid automatic switching setting');
        ensure(matches($vm, VM) && matches($profile->gpu, NAME) && isset($cfg->gpus->{$profile->gpu})
            && !$cfg->gpus->{$profile->gpu}->protected, 'Invalid VM or protected/missing GPU');
        ensure(is_array($profile->containers) && count($profile->containers) <= 64, 'Invalid container list');
        $names = [];
        foreach ($profile->containers as $item) {
            fields($item, 'name policy after');
            ensure(matches($item->name, NAME) && !isset($names[$item->name])
                && in_array($item->policy, ['restore', 'stop', 'leave'], true), 'Invalid or duplicate container');
            $names[$item->name] = true;
            $assignments[$item->name][$profile->gpu] = true;
            if ($item->policy !== 'leave') {
                $stoppable[$item->name] = true;
            }
        }
        foreach ($profile->containers as $item) {
            ensure(is_array($item->after), 'Invalid dependencies');
            foreach ($item->after as $dependency) {
                ensure(matches($dependency, NAME) && isset($names[$dependency]), 'Unknown dependency');
            }
        }
        ordered($profile->containers);
    }
    foreach ($stoppable as $name => $_) {
        ensure(count($assignments[$name]) === 1, 'Container with a stop policy belongs to multiple GPUs: ' . $name);
    }
    return $cfg;
}

function encode(mixed $value, bool $pretty = false): string
{
    return json_encode($value, JSON_THROW_ON_ERROR | JSON_INVALID_UTF8_SUBSTITUTE | ($pretty ? JSON_PRETTY_PRINT : 0));
}

function canonical(mixed $value): mixed
{
    if ($value instanceof \stdClass) {
        $entries = (array)$value;
        ksort($entries, SORT_STRING);
        return (object)array_map(__NAMESPACE__ . '\\canonical', $entries);
    }
    return is_array($value) ? array_map(__NAMESPACE__ . '\\canonical', $value) : $value;
}

function same(mixed $left, mixed $right): bool
{
    return encode(canonical($left)) === encode(canonical($right));
}

function readText(string $path): string
{
    $contents = @file_get_contents($path);
    ensure($contents !== false, 'Cannot read ' . $path);
    return $contents;
}

function readJson(string $path): mixed
{
    return json_decode(readText($path), false, 128, JSON_THROW_ON_ERROR);
}

function hasPath(string $path): bool
{
    return is_link($path) || file_exists($path);
}

function directory(string $path, int $mode = 0700): void
{
    ensure(!is_link($path) && (is_dir($path) || @mkdir($path, $mode, true) || is_dir($path)),
        'Cannot create safe directory ' . $path);
}

function syncDirectory(string $path): void
{
    $stream = @fopen($path, 'r');
    ensure($stream !== false, 'Cannot open directory ' . $path);
    try {
        ensure(fsync($stream), 'Cannot sync directory ' . $path);
    } finally {
        fclose($stream);
    }
}

function putFile(string $path, string $contents, int $mode = 0600): void
{
    $parent = dirname($path);
    directory($parent);
    $tmp = tempnam($parent, '.gsm-');
    ensure($tmp !== false, 'Cannot stage ' . $path);
    try {
        $stream = fopen($tmp, 'wb');
        ensure($stream !== false, 'Cannot open staged file');
        try {
            ensure(chmod($tmp, $mode) && fwrite($stream, $contents) === strlen($contents)
                && fflush($stream) && fsync($stream), 'Cannot write ' . $path);
        } finally {
            fclose($stream);
        }
        ensure(rename($tmp, $path), 'Cannot replace ' . $path);
        syncDirectory($parent);
    } finally {
        if (is_file($tmp)) {
            unlink($tmp);
        }
    }
}

function writeJson(string $path, mixed $value): void
{
    putFile($path, encode(canonical($value), true) . "\n");
}

function removeFile(string $path): void
{
    if (hasPath($path)) {
        ensure(unlink($path), 'Cannot remove ' . $path);
    }
}

function uuid(): string
{
    $bytes = random_bytes(16);
    $bytes[6] = chr((ord($bytes[6]) & 0x0f) | 0x40);
    $bytes[8] = chr((ord($bytes[8]) & 0x3f) | 0x80);
    return vsprintf('%s%s-%s-%s-%s-%s%s%s', str_split(bin2hex($bytes), 4));
}

final class Store
{
    public string $settings;
    public string $leases;

    public function __construct(public string $config = CONFIG, public string $run = RUN)
    {
        $this->settings = $config . '/sharing.json';
        $this->leases = $config . '/transactions';
    }

    public function cfg(): \stdClass
    {
        return validate(readJson($this->settings));
    }

    public function journal(string $gpu): string
    {
        ensure(matches($gpu, NAME), 'Invalid GPU key');
        return $this->leases . '/' . $gpu . '.json';
    }

    public function journals(): array
    {
        if (!hasPath($this->leases)) {
            return [];
        }
        ensure(!is_link($this->leases) && is_dir($this->leases) && is_readable($this->leases),
            'Cannot inspect pending transactions');
        $paths = glob($this->leases . '/*.json', GLOB_ERR);
        ensure($paths !== false, 'Cannot enumerate pending transactions');
        return $paths;
    }

    public function locked(string $name, callable $work, bool $shared = false): mixed
    {
        directory($this->run);
        $path = $this->run . '/' . $name . '.lock';
        ensure(!is_link($path), 'Unexpected lock symlink');
        $lock = fopen($path, 'c');
        ensure($lock !== false, 'Cannot open ' . $name . ' lock');
        try {
            $end = hrtime(true) + 4_000_000_000;
            while (!flock($lock, ($shared ? LOCK_SH : LOCK_EX) | LOCK_NB)) {
                ensure(hrtime(true) < $end, 'Another operation holds ' . $name);
                usleep(50_000);
            }
            clearstatcache(true, $path);
            $current = @stat($path);
            $opened = fstat($lock);
            ensure($current && $opened && $current['ino'] === $opened['ino'] && $current['dev'] === $opened['dev'],
                'Runtime was removed during this operation');
            return $work();
        } finally {
            fclose($lock);
        }
    }

    public function log(string $gpu, ?string $vm, string $state, string $message): void
    {
        try {
            $this->locked('history', function () use ($gpu, $vm, $state, $message) {
                $history = $this->history();
                $history[] = (object)['time' => time(), 'gpu' => $gpu, 'vm' => $vm, 'state' => $state, 'message' => $message];
                writeJson($this->run . '/history.json', array_slice($history, -200));
                if ($state === 'HOST_AVAILABLE' || $state === 'ERROR') {
                    writeJson($this->config . '/operation-history.json', array_slice($history, -100));
                }
            });
        } catch (\Throwable $error) {
            syslog(LOG_WARNING, 'GPU Switch Manager could not write operation history: ' . $error->getMessage());
        }
    }

    public function history(): array
    {
        foreach ([$this->run . '/history.json', $this->config . '/operation-history.json'] as $path) {
            if (is_file($path)) {
                try {
                    $history = readJson($path);
                    if (is_array($history)) {
                        return array_values(array_filter($history, fn($entry) => $entry instanceof \stdClass));
                    }
                } catch (\JsonException | Refused) {
                    syslog(LOG_WARNING, 'GPU Switch Manager could not read operation history: ' . $path);
                }
            }
        }
        return [];
    }

    public function change(mixed $cfg): void
    {
        ensure(!$this->journals(), 'Resolve outstanding transactions before editing configuration');
        writeJson($this->settings, validate($cfg));
    }
}
