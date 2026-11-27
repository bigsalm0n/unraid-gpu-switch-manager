<?php
declare(strict_types=1);

namespace Gsm;

require_once __DIR__ . '/model.php';

function command(array $args, int $timeout = 12): string
{
    [$program, $operation] = $args + [1 => ''];
    $stdout = tmpfile();
    $stderr = tmpfile();
    ensure($stdout !== false && $stderr !== false, 'Cannot stage command output');
    try {
        $process = @proc_open(['timeout', '--kill-after=2', (string)$timeout, ...$args],
            [0 => ['file', '/dev/null', 'r'], 1 => $stdout, 2 => $stderr], $pipes, null,
            ['LC_ALL' => 'C', 'PATH' => '/usr/local/sbin:/usr/sbin:/sbin:/usr/local/bin:/usr/bin:/bin']);
        ensure(is_resource($process), 'Cannot execute ' . $program);
        $status = proc_close($process);
        rewind($stderr);
        $error = trim((string)stream_get_contents($stderr, 700));
        ensure($status === 0, "$program $operation: " . (in_array($status, [124, 137], true) ? 'timed out' : ($error ?: $status)));
        rewind($stdout);
        $output = stream_get_contents($stdout);
        ensure($output !== false, 'Cannot read output from ' . $program);
        return $output;
    } finally {
        fclose($stdout);
        fclose($stderr);
    }
}

function xml(string $input): \SimpleXMLElement
{
    ensure(strlen($input) <= 4 * 1024 * 1024, 'XML exceeds size limit');
    $parsed = simplexml_load_string($input, \SimpleXMLElement::class, LIBXML_NONET | LIBXML_NOERROR | LIBXML_NOWARNING);
    ensure($parsed !== false, 'Invalid XML');
    return $parsed;
}

function paths(string $pattern): array
{
    $result = glob($pattern);
    ensure($result !== false, 'Cannot enumerate ' . $pattern);
    return $result;
}

function field(string $path): string
{
    return trim(readText($path));
}

function basenames(array $paths): array
{
    return array_map('basename', $paths);
}

function driver(string $device): ?string
{
    $target = @readlink($device . '/driver');
    return $target === false ? null : basename($target);
}

function sysfsDeviceNumber(string $path): int
{
    $number = field($path);
    ensure(preg_match('/^([0-9]{1,4}):([0-9]{1,7})$/D', $number, $parts) === 1,
        'Invalid device number at ' . $path);
    $major = (int)$parts[1];
    $minor = (int)$parts[2];
    ensure($major <= 4095 && $minor <= 1048575, 'Out-of-range device number at ' . $path);
    // Linux encodes the low eight minor bits below the major and the remainder above it.
    return (($minor & ~255) << 12) | ($major << 8) | ($minor & 255);
}

function devicePaths(object $container): array
{
    $devices = array_column($container->HostConfig->Devices ?? [], 'PathOnHost');
    foreach ($container->Mounts ?? [] as $mount) {
        if ($mount->Type === 'bind') {
            $devices[] = $mount->Source;
        }
    }
    return $devices;
}

function gpuRequest(object $request): bool
{
    if (in_array($request->Driver ?? '', ['nvidia', 'cdi'], true)) {
        return true;
    }
    foreach ($request->Capabilities ?? [] as $capabilities) {
        if (in_array('gpu', $capabilities, true)) {
            return true;
        }
    }
    return false;
}

class Hardware
{
    public string $pci;
    private \Closure $runner;

    public function __construct(private string $sys = '/sys', private string $proc = '/proc',
        private string $dev = '/dev', ?callable $runner = null)
    {
        $this->pci = $sys . '/bus/pci/devices';
        $this->runner = $runner === null ? command(...) : \Closure::fromCallable($runner);
    }

    public function run(array $args, int $timeout = 12): string
    {
        return ($this->runner)($args, $timeout);
    }

    public function info(string $address): array
    {
        ensure(matches($address, PCI), 'Invalid PCI address');
        clearstatcache(true);
        $slot = $this->pci . '/' . $address;
        ensure(str_starts_with(field($slot . '/class'), '0x03'), 'Expected graphics controller at ' . $address);
        $functions = $drivers = $groups = $neighbors = $resetPeers = [];
        foreach (paths(substr($slot, 0, -1) . '?') as $device) {
            $bdf = basename($device);
            $signature = [];
            foreach (['class', 'vendor', 'device', 'subsystem_vendor', 'subsystem_device'] as $attribute) {
                $signature[] = preg_replace('/^0x/', '', field($device . '/' . $attribute));
            }
            $functions[substr($bdf, -1)] = implode(':', $signature);
            $drivers[$bdf] = driver($device);
            $group = realpath($device . '/iommu_group');
            $groups[$bdf] = $group === false ? null : basename($group);
            $neighbors[$bdf] = basenames(paths($device . '/iommu_group/devices/*'));
            $physical = realpath($device);
            ensure($physical !== false, 'PCI device disappeared: ' . $bdf);
            $bridge = dirname($physical);
            $resetPeers[$bdf] = matches(basename($bridge), PCI)
                ? array_values(array_filter(basenames(paths($bridge . '/*')), fn($peer) => matches($peer, PCI))) : [];
        }
        ensure(array_key_exists($address, $drivers), 'PCI device disappeared: ' . $address);
        $data = ['pci' => $address, 'functions' => (object)$functions, 'drivers' => $drivers,
            'groups' => $groups, 'neighbors' => $neighbors, 'reset_peers' => $resetPeers,
            'uuid' => null, 'model' => null, 'minor' => null, 'temperature' => null, 'memory_used' => null,
            'memory_total' => null, 'utilization' => null, 'processes' => [], 'error' => null,
            'boot_display' => is_file($slot . '/boot_vga') && field($slot . '/boot_vga') === '1'];
        if ($drivers[$address] === 'nvidia') {
            try {
                $report = xml($this->run(['nvidia-smi', '-i', $address, '-q', '-x']));
                ensure(count($report->gpu) === 1, 'Expected one NVIDIA device');
                $gpu = $report->gpu;
                $bus = strtolower((string)$gpu->pci->pci_bus_id);
                ensure(preg_match('/^([0-9a-f]{4,8}):(.*)$/D', $bus, $parts) === 1
                    && sprintf('%04x:%s', hexdec($parts[1]), $parts[2]) === $address, 'NVIDIA bus ID mismatch');
                foreach (['uuid' => 'uuid', 'model' => 'product_name', 'minor' => 'minor_number',
                    'temperature' => 'temperature/gpu_temp', 'memory_used' => 'fb_memory_usage/used',
                    'memory_total' => 'fb_memory_usage/total', 'utilization' => 'utilization/gpu_util'] as $key => $tag) {
                    $values = $gpu->xpath($tag);
                    $data[$key] = $values ? (string)$values[0] : null;
                }
                foreach ($gpu->processes->process_info ?? [] as $process) {
                    $data['processes'][] = ['pid' => (string)$process->pid, 'kind' => (string)$process->type,
                        'name' => (string)$process->process_name];
                }
            } catch (Refused $error) {
                $data['error'] = $error->getMessage();
            }
        }
        return $data;
    }

    public function discover(): array
    {
        ensure(is_dir($this->pci) && is_readable($this->pci), 'PCI device inventory is unavailable');
        $result = [];
        foreach (paths($this->pci . '/*') as $path) {
            $address = basename($path);
            if (!matches($address, PCI) || !str_starts_with(field($path . '/class'), '0x03')) {
                continue;
            }
            $info = $this->info($address);
            $labels = [];
            foreach (explode("\n", $this->run(['lspci', '-Dvmmnn', '-s', $address])) as $line) {
                if (str_contains($line, ":\t")) {
                    [$key, $value] = explode(":\t", $line, 2);
                    $labels[$key] = trim($value);
                }
            }
            $info['description'] = trim(($labels['Vendor'] ?? '') . ' ' . ($labels['Device'] ?? ''));
            $info['board_vendor'] = $labels['SVendor'] ?? null;
            $result[] = $info;
        }
        return $result;
    }

    public function snapshot(string $address, bool $protected): \stdClass
    {
        $data = $this->info($address);
        ensure($protected || (matches($data['uuid'], GPU_UUID) && !$data['error']), 'Shared GPU requires a working NVIDIA UUID');
        return (object)['pci' => $address, 'uuid' => $data['uuid'], 'functions' => $data['functions'], 'protected' => $protected];
    }

    public function verify(object $gpu, bool $host = true): array
    {
        ensure(!$gpu->protected, 'GPU is protected');
        $data = $this->info($gpu->pci);
        ensure(same($data['functions'], $gpu->functions), 'PCI fingerprint changed; rediscover and remap');
        ensure(!$data['boot_display'], 'Boot display is reserved for the host');
        $addresses = array_keys($data['drivers']);
        foreach ($data['neighbors'] as $address => $members) {
            ensure(in_array($address, $members, true) && !array_diff($members, $addresses), 'Unsafe IOMMU group at ' . $address);
            $peers = $data['reset_peers'][$address];
            ensure(in_array($address, $peers, true) && !array_diff($peers, $addresses), 'Cannot isolate an upstream bridge reset at ' . $address);
            $number = substr($address, -1);
            ensure($address === $gpu->pci || str_starts_with($data['functions']->$number, '0403'), 'Unapproved sibling function at ' . $address);
        }
        if ($host) {
            ensure($data['uuid'] === $gpu->uuid && $data['uuid'] && !$data['error'], 'Host GPU UUID unavailable or changed');
            ensure($data['drivers'][$gpu->pci] === 'nvidia' && !in_array(null, $data['drivers'], true)
                && !in_array('vfio-pci', $data['drivers'], true), 'Expected a host driver on every function');
            foreach ($addresses as $address) {
                $override = $this->pci . '/' . $address . '/driver_override';
                ensure(!file_exists($override) || in_array(field($override), ['', '(null)'], true),
                    'Review existing PCI driver override at ' . $address);
            }
        }
        return $data;
    }

    public function clients(array $data): array
    {
        ensure(is_dir($this->proc) && is_readable($this->proc), 'Process inventory is unavailable');
        $nodes = [];
        $numbers = [];
        if ($data['drivers'][$data['pci']] === 'nvidia') {
            ensure(ctype_digit((string)$data['minor']), 'NVIDIA minor number unavailable');
            $nodes[] = $this->dev . '/nvidia' . $data['minor'];
        }
        foreach ($data['drivers'] as $address => $_) {
            $slot = $this->pci . '/' . $address;
            foreach (paths($slot . '/drm/*/dev') as $node) {
                // DRM can publish a minor in sysfs without creating its host /dev node.
                $numbers[sysfsDeviceNumber($node)] = true;
            }
            foreach (paths($slot . '/sound/card[0-9]*') as $card) {
                $number = substr(basename($card), 4);
                array_push($nodes, ...paths($this->dev . '/snd/*C' . $number . '[!0-9]*'));
                $nodes[] = $this->dev . '/snd/controlC' . $number;
            }
        }
        foreach ($nodes as $node) {
            $stat = @stat($node);
            ensure($stat && ($stat['mode'] & 0170000) === 0020000, 'Invalid GPU device node: ' . $node);
            $numbers[$stat['rdev']] = true;
        }
        ensure((bool)$numbers, 'Target device nodes unavailable');
        $pids = [];
        foreach ($data['processes'] as $process) {
            if (ctype_digit($process['pid'])) {
                $pids[(int)$process['pid']] = true;
            }
        }
        $deadline = hrtime(true) + 10_000_000_000;
        foreach (paths($this->proc . '/[0-9]*') as $proc) {
            ensure(hrtime(true) < $deadline, 'Timed out checking target device handles');
            $fdPath = $proc . '/fd';
            $directory = @opendir($fdPath);
            if ($directory === false) {
                clearstatcache(true, $fdPath);
                ensure(!is_dir($fdPath), 'Cannot inspect process descriptors: ' . basename($proc));
                continue;
            }
            try {
                while (($fd = readdir($directory)) !== false) {
                    ensure(hrtime(true) < $deadline, 'Timed out checking target device handles');
                    if (!ctype_digit($fd)) {
                        continue;
                    }
                    $stat = @stat($fdPath . '/' . $fd);
                    if ($stat && ($stat['mode'] & 0170000) === 0020000 && isset($numbers[$stat['rdev']])) {
                        $pids[(int)basename($proc)] = true;
                        break;
                    }
                }
            } finally {
                closedir($directory);
            }
        }
        $result = array_keys($pids);
        sort($result, SORT_NUMERIC);
        return $result;
    }

    public function containers(): array
    {
        $ids = preg_split('/\s+/', trim($this->run(['docker', 'ps', '-aq', '--no-trunc'])), -1, PREG_SPLIT_NO_EMPTY);
        foreach ($ids as $id) {
            ensure(matches($id, '[0-9a-f]{64}'), 'Invalid Docker inventory identity');
        }
        $rows = $ids ? json_decode($this->run(['docker', 'inspect', ...$ids]), false, 128, JSON_THROW_ON_ERROR) : [];
        ensure(is_array($rows) && count($rows) === count($ids), 'Incomplete Docker inventory');
        $result = [];
        foreach ($rows as $row) {
            $name = ltrim($row->Name, '/');
            ensure(matches($name, NAME) && in_array($row->Id, $ids, true) && is_bool($row->State->Running),
                'Invalid Docker container record');
            $result[$name] = $row;
        }
        return $result;
    }

    private function visible(object $container): array
    {
        $visible = [];
        foreach ($container->Config->Env ?? [] as $entry) {
            if (str_starts_with($entry, 'NVIDIA_VISIBLE_DEVICES=')) {
                array_push($visible, ...explode(',', substr($entry, strlen('NVIDIA_VISIBLE_DEVICES='))));
            }
        }
        return $visible;
    }

    public function selectors(object $container): \stdClass
    {
        $tokens = $this->visible($container);
        foreach ($container->HostConfig->DeviceRequests ?? [] as $request) {
            if (gpuRequest($request)) {
                array_push($tokens, ...($request->DeviceIDs ?? []));
            }
        }
        $references = [];
        foreach (array_unique($tokens) as $token) {
            $index = preg_replace('/^nvidia\.com\/gpu=/', '', trim($token));
            if (ctype_digit($index)) {
                $uuid = trim($this->run(['nvidia-smi', '-i', $index, '--query-gpu=uuid', '--format=csv,noheader']));
                ensure(matches($uuid, GPU_UUID), 'Cannot resolve NVIDIA index ' . $index);
                $references['index:' . $index] = $uuid;
            }
        }
        foreach (devicePaths($container) as $path) {
            if (matches($path, '/dev/nvidia[0-9]+')) {
                $ids = [];
                foreach ($this->discover() as $device) {
                    if ($device['minor'] === substr($path, strlen('/dev/nvidia'))) {
                        $ids[] = $device['uuid'];
                    }
                }
                ensure(count($ids) === 1 && matches($ids[0], GPU_UUID), 'Cannot resolve Docker device ' . $path);
                $references[$path] = $ids[0];
            } elseif (str_starts_with($path, '/dev/dri/')) {
                $node = realpath($this->dev . substr($path, 4));
                $slot = $node === false ? false : realpath($this->sys . '/class/drm/' . basename($node) . '/device');
                ensure($slot !== false && matches(basename($slot), PCI), 'Cannot resolve DRM mapping ' . $path);
                $references[$path] = basename($slot);
            }
        }
        return (object)$references;
    }

    public function owner(int $pid, array $containers): ?string
    {
        $path = $this->proc . '/' . $pid . '/cgroup';
        $group = @file_get_contents($path);
        if ($group === false) {
            ensure(!file_exists($path), 'Cannot inspect cgroup for PID ' . $pid);
            return null;
        }
        foreach ($containers as $name => $container) {
            if (str_contains($group, $container->Id)) {
                return (string)$name;
            }
        }
        return null;
    }

    public function processIds(object $container): array
    {
        $lines = preg_split('/\R/', trim($this->run(['docker', 'top', $container->Id, '-eo', 'pid'])));
        ensure(trim(array_shift($lines)) === 'PID', 'Cannot identify processes for ' . $container->Name);
        $pids = [];
        foreach ($lines as $line) {
            ensure(ctype_digit(trim($line)), 'Cannot identify processes for ' . $container->Name);
            $pids[] = (int)trim($line);
        }
        return $pids;
    }

    public function assignments(array $data, array $containers, array $clients): array
    {
        $owners = array_map(fn($pid) => $this->owner($pid, $containers), $clients);
        $found = [];
        foreach ($containers as $name => $container) {
            $host = $container->HostConfig;
            $nvidia = $data['drivers'][$data['pci']] === 'nvidia';
            $tokens = $this->visible($container);
            $reasons = [];
            if ($nvidia && ($host->Runtime ?? '') === 'nvidia' && !$tokens) {
                $reasons[] = 'NVIDIA runtime with unspecified visibility';
            }
            if (!empty($host->Privileged)) {
                $reasons[] = 'privileged device access';
            }
            foreach ($host->DeviceRequests ?? [] as $request) {
                if (gpuRequest($request)) {
                    array_push($tokens, ...(!empty($request->DeviceIDs) ? $request->DeviceIDs : ['all']));
                }
            }
            foreach (devicePaths($container) as $path) {
                if (in_array($path, ['/dev', '/dev/dri', '/dev/nvidia' . $data['minor']], true)) {
                    $reasons[] = $path;
                } elseif (str_starts_with($path, '/dev/dri/')) {
                    $node = realpath($this->dev . substr($path, 4)) ?: $path;
                    $device = realpath($this->sys . '/class/drm/' . basename($node) . '/device');
                    if ($device === false || basename($device) === $data['pci'] || !matches(basename($device), PCI)) {
                        $reasons[] = $path;
                    }
                }
            }
            foreach ($nvidia ? array_unique(array_map('trim', $tokens)) : [] as $token) {
                if (!in_array($token, ['', 'none', 'void'], true)
                    && ($token === $data['uuid'] || !matches($token, GPU_UUID))) {
                    $reasons[] = 'visible ' . $token;
                }
            }
            if (in_array((string)$name, $owners, true)) {
                $reasons[] = 'active client';
            }
            if ($reasons) {
                $found[$name] = $reasons;
            }
        }
        return $found;
    }
}
