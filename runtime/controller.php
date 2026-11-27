<?php
declare(strict_types=1);

namespace Gsm;

require_once __DIR__ . '/lifecycle.php';

const HOOK = '/etc/libvirt/hooks/qemu.d/60-gpu-switch-manager';

function lines(string $output): array
{
    return preg_split('/\R/', trim($output), -1, PREG_SPLIT_NO_EMPTY);
}

function virsh(Manager $manager, string ...$args): string
{
    return $manager->hw->run(['virsh', '-c', 'qemu:///system', ...$args],
        in_array($args[0], ['start', 'create', 'destroy'], true) ? 210 : 15);
}

function revision(object $cfg): string
{
    return hash('sha256', encode(canonical($cfg)));
}

function status(Manager $manager, bool $diagnostics): array
{
    $cfg = $manager->s->cfg();
    $errors = $devices = $vms = $active = $domains = $leases = [];
    $containers = null;
    try {
        $devices = $manager->hw->discover();
    } catch (\Throwable $error) {
        $errors[] = 'GPU discovery failed: ' . $error->getMessage();
    }
    try {
        $containers = $manager->hw->containers();
    } catch (\Throwable $error) {
        $errors[] = 'Docker discovery failed: ' . $error->getMessage();
    }
    try {
        $vms = lines(virsh($manager, 'list', '--all', '--name'));
        $domains = lines(virsh($manager, 'list', '--all'));
        foreach (lines(virsh($manager, 'list', '--uuid')) as $uid) {
            ensure(matches($uid, DOMAIN_UUID), 'Invalid domain identity in VM inventory');
            [$owner, , $used] = domain(virsh($manager, 'dumpxml', $uid));
            foreach ($used as $address => $_) {
                $active[$address] = $owner;
            }
        }
    } catch (\Throwable $error) {
        $errors[] = 'VM discovery failed: ' . $error->getMessage();
    }
    $history = $manager->s->history();
    foreach ($manager->s->journals() as $path) {
        $key = basename($path, '.json');
        try {
            $leases[$key] = $manager->lease($key, $cfg);
        } catch (\Throwable $error) {
            $leases[$key] = (object)['state' => 'ERROR', 'message' => 'Invalid transaction: ' . $error->getMessage()];
            $errors[] = 'Inspect ' . basename($path) . ': ' . $error->getMessage();
        }
    }
    $enrolled = [];
    foreach ($cfg->gpus as $key => $gpu) {
        $enrolled[$gpu->pci] = $key;
    }
    foreach ($devices as &$data) {
        $pci = $data['pci'];
        $key = $enrolled[$pci] ?? null;
        $gpu = $key === null ? null : $cfg->gpus->$key;
        $lease = $key === null ? null : ($leases[$key] ?? null);
        $owner = array_intersect_key($active, $data['drivers']);
        $driver = $data['drivers'][$pci];
        $data += ['key' => $key, 'protected' => $gpu->protected ?? false, 'enrolled_uuid' => $gpu->uuid ?? null,
            'owner' => $owner ? implode(', ', array_unique($owner)) : null,
            'transaction' => $lease, 'clients' => null, 'containers' => null, 'last' => null];
        foreach (array_reverse($history) as $entry) {
            if ($key !== null && ($entry->gpu ?? null) === $key) {
                $data['last'] = $entry;
                break;
            }
        }
        $data['state'] = $lease->state ?? ($driver === 'vfio-pci' ? 'VFIO_BOUND' : 'HOST_AVAILABLE');
        if ($driver && $driver !== 'vfio-pci' && !$data['error']) {
            try {
                $data['clients'] = $manager->hw->clients($data);
                if ($containers !== null) {
                    $found = $manager->hw->assignments($data, $containers, $data['clients']);
                    $data['containers'] = (object)array_filter($found, fn($name) => running($containers[$name]), ARRAY_FILTER_USE_KEY);
                }
                if (!$lease && $data['clients']) {
                    $data['state'] = 'HOST_IN_USE';
                }
            } catch (\Throwable $error) {
                $data['error'] = $error->getMessage();
            }
        }
        if ($gpu && (!same($gpu->functions, $data['functions']) || ($gpu->uuid && $driver !== 'vfio-pci' && $data['uuid'] !== $gpu->uuid))) {
            $data['error'] = 'Enrolled identity differs; verify and remap';
        }
        if ($data['error'] || !$driver) {
            $data['state'] = 'ERROR';
        }
    }
    unset($data);
    $release = __DIR__ . '/release.json';
    $names = $containers === null ? null : array_map('strval', array_keys($containers));
    if ($names !== null) {
        sort($names, SORT_NATURAL | SORT_FLAG_CASE);
    }
    $report = ['devices' => $devices, 'errors' => $errors, 'config' => $cfg, 'revision' => revision($cfg),
        'transactions' => (object)$leases, 'release' => is_file($release) ? readJson($release) : null,
        'containers' => $names, 'vms' => $vms, 'hook_installed' => is_executable(HOOK) && hash_file('sha256', HOOK)
            === hash_file('sha256', __DIR__ . '/qemu-hook'),
        'array_stopping' => is_file($manager->s->run . '/array-stopping')];
    if ($diagnostics) {
        $report['commands'] = dependencies();
        $report['kernel'] = php_uname('r');
        $report['vm_states'] = $domains;
        $report['docker_states'] = $containers === null ? null : (object)array_map(
            fn($container) => ['running' => running($container), 'status' => $container->State->Status ?? null,
                'id' => $container->Id], $containers);
        $report['unresolved'] = array_values(array_diff_key($enrolled, array_column($devices, null, 'pci')));
    }
    return $report;
}

function dependencies(): array
{
    $result = ['php' => PHP_VERSION, 'simplexml' => extension_loaded('simplexml'), 'dom' => extension_loaded('dom'),
        'posix' => extension_loaded('posix')];
    foreach (['docker', 'virsh', 'lspci', 'nvidia-smi', 'timeout', 'mountpoint'] as $name) {
        $result[$name] = null;
        foreach (['/usr/local/sbin', '/usr/sbin', '/sbin', '/usr/local/bin', '/usr/bin', '/bin'] as $dir) {
            if (is_executable("$dir/$name")) {
                $result[$name] = "$dir/$name";
                break;
            }
        }
    }
    return $result;
}

function proof(Manager $manager): array
{
    $uid = uuid();
    $name = 'gsm-probe-' . $uid;
    $xml = "<domain type='kvm'><name>$name</name><uuid>$uid</uuid><memory unit='MiB'>128</memory><vcpu>1</vcpu><os><type arch='x86_64'>hvm</type></os><devices/></domain>";
    return $manager->s->locked('probe', function () use ($manager, $uid, $name, $xml) {
        $marker = $manager->s->run . '/' . $name;
        $spec = $marker . '.xml';
        try {
            putFile($marker, $uid);
            putFile($spec, $xml);
            try {
                virsh($manager, 'create', $spec, '--paused');
            } catch (Refused $error) {
                ensure(readText($marker) === 'observed', 'Libvirt did not invoke this plugin’s prepare hook: ' . $error->getMessage());
                return ['verified' => true, 'event' => 'prepare/begin'];
            }
            try {
                virsh($manager, 'destroy', $uid);
            } catch (Refused $error) {
                throw new Refused("Inspect paused probe $name ($uid); cleanup failed: " . $error->getMessage());
            }
            throw new Refused('Probe reached paused startup; hook is inactive. Check libvirt hook registration.');
        } finally {
            removeFile($spec);
            removeFile($marker);
        }
    });
}

function assign(Manager $manager, object $cfg, string $key, string $vm, string $uid): array
{
    ensure(!$manager->lease($key, $cfg), 'GPU has an outstanding transaction');
    $data = $manager->target($cfg, $key);
    ensure(trim(virsh($manager, 'domstate', $uid)) === 'shut off', 'Shut down the selected VM before changing its definition');
    $original = virsh($manager, 'dumpxml', '--inactive', '--security-info', $uid);
    [$name, $identity, $devices] = domain($original);
    ensure($name === $vm && $identity === $uid, 'VM definition changed');
    $expected = array_fill_keys(array_keys($data['drivers']), true);
    ensure(!array_diff_key($devices, $expected) && !in_array(false, $devices, true), 'Review conflicting PCI hostdev entries in the VM editor');
    $manager->guard($cfg, $vm, $expected);
    if ($devices !== $expected) {
        $doc = new \DOMDocument();
        ensure($doc->loadXML($original, LIBXML_NONET), 'Cannot load VM definition');
        $parent = (new \DOMXPath($doc))->query('/domain/devices')->item(0);
        ensure($parent !== null, 'VM definition has no devices element');
        foreach (array_diff_key($expected, $devices) as $address => $_) {
            $hostdev = $parent->appendChild($doc->createElement('hostdev'));
            foreach (['mode' => 'subsystem', 'type' => 'pci', 'managed' => 'yes'] as $field => $value) {
                $hostdev->setAttribute($field, $value);
            }
            $hostdev->appendChild($doc->createElement('driver'))->setAttribute('name', 'vfio');
            $source = $hostdev->appendChild($doc->createElement('source'));
            $node = $source->appendChild($doc->createElement('address'));
            foreach (['domain' => substr($address, 0, 4), 'bus' => substr($address, 5, 2),
                'slot' => substr($address, 8, 2), 'function' => substr($address, -1)] as $field => $value) {
                $node->setAttribute($field, '0x' . $value);
            }
        }
        $spec = $manager->s->run . '/definition-' . $uid . '.xml';
        try {
            putFile($spec, $doc->saveXML());
            ensure($original === virsh($manager, 'dumpxml', '--inactive', '--security-info', $uid)
                && trim(virsh($manager, 'domstate', $uid)) === 'shut off', 'VM changed while editing; refresh');
            virsh($manager, 'define', '--validate', $spec);
        } finally {
            removeFile($spec);
        }
    }
    ensure(domain(virsh($manager, 'dumpxml', '--inactive', $uid)) === [$vm, $uid, $expected],
        'Persistent hostdev verification failed');
    return ['vm' => $vm, 'managed_devices' => array_keys($expected)];
}

function recover(Manager $manager, string $key): array
{
    $cfg = $manager->s->cfg();
    ensure(matches($key, NAME) && isset($cfg->gpus->$key), 'Unknown GPU');
    $before = $manager->lease($key, $cfg);
    if (!$before) {
        return ['pending' => false];
    }
    // A libvirt RPC can wait for a hook. Do not hold a GPU lock during these calls.
    if (in_array($before->domain, lines(virsh($manager, 'list', '--all', '--uuid')), true)) {
        ensure(trim(virsh($manager, 'domstate', $before->domain)) === 'shut off', 'Owner domain is not shut off');
        try {
            $job = virsh($manager, 'domjobinfo', $before->domain);
            ensure(preg_match('/^Job type:[ \t]*None\s*$/m', $job) === 1, 'Owner domain has a pending libvirt job');
        } catch (Refused $error) {
            if (!str_contains($error->getMessage(), 'domain is not running')) {
                throw $error;
            }
        }
    }
    foreach (lines(virsh($manager, 'list', '--uuid')) as $uid) {
        ensure(matches($uid, DOMAIN_UUID), 'Invalid running VM identity');
        [, , $devices] = domain(virsh($manager, 'dumpxml', $uid));
        ensure($uid !== $before->domain && !array_intersect_key($devices, (array)$before->drivers),
            'A running domain still owns this assignment');
    }
    return $manager->s->locked('gpu-' . $key, function () use ($manager, $key, $cfg, $before) {
        $current = $manager->lease($key, $cfg);
        ensure(same($current, $before), 'Transaction changed during reconciliation; refresh diagnostics');
        $manager->restore($key, $current);
        return ['restored' => $key];
    });
}

function configure(Manager $manager, string $action, array $args, string $input): object
{
    $cfg = $action === 'import' ? validate(json_decode($input, false, 128, JSON_THROW_ON_ERROR)) : null;
    $verified = $cfg && $cfg->enabled && !$manager->s->cfg()->enabled;
    if ($verified) {
        proof($manager);
    }
    return $manager->s->locked('maintenance', function () use ($manager, $action, $args, $cfg, $verified) {
        if ($action === 'init') {
            if (!is_file($manager->s->settings)) {
                $manager->s->change(defaults());
            }
            return $manager->s->cfg();
        }
        $current = $manager->s->cfg();
        if ($action === 'import') {
            ensure(!$args || ($args[0] === '--revision' && $args[1] === revision($current)),
                'Configuration was edited; reload before saving');
            ensure(!$cfg->enabled || $current->enabled || $verified, 'Retry enabling after the concurrent configuration edit');
            foreach ($cfg->gpus as $key => $gpu) {
                if (!same($current->gpus->$key ?? null, $gpu) || ($cfg->enabled && !$current->enabled)) {
                    ensure(same($manager->hw->snapshot($gpu->pci, $gpu->protected), $gpu), 'Identity mismatch for ' . $key);
                }
            }
        } else {
            [$key, $address, $role] = $args;
            ensure(matches($key, NAME) && matches($address, PCI) && in_array($role, ['protected', 'shared'], true), 'Invalid enrollment');
            $cfg = $current;
            $cfg->gpus->$key = $manager->hw->snapshot($address, $role === 'protected');
        }
        $manager->s->change($cfg);
        return $cfg;
    });
}

function vmAction(Manager $manager, object $cfg, string $action, string $vm): array
{
    ensure(matches($vm, VM) && isset($cfg->profiles->$vm), 'Select a configured VM');
    $key = $cfg->profiles->$vm->gpu;
    [$name, $uid, $devices] = domain(virsh($manager, 'dumpxml', '--inactive', $vm));
    ensure($name === $vm && matches($uid, DOMAIN_UUID), 'Domain identity mismatch');
    return $manager->s->locked('definition-' . $uid, function () use ($manager, $cfg, $action, $key, $vm, $uid, $devices) {
        if ($action === 'assign') {
            proof($manager);
            return assign($manager, $cfg, $key, $vm, $uid);
        }
        if ($action === 'shutdown') {
            $journal = $manager->lease($key, $cfg);
            ensure($journal && $journal->domain === $uid && $journal->vm === $vm && $journal->state === 'VM_ACTIVE',
                'No matching active transaction');
            virsh($manager, 'shutdown', $uid);
            return ['vm' => $vm, 'result' => 'Guest shutdown requested; release is pending'];
        }
        ensure(trim(virsh($manager, 'domstate', $uid)) === 'shut off', 'The selected VM must be shut off');
        $plan = $manager->s->locked('gpu-' . $key, function () use ($manager, $cfg, $vm, $key, $uid, $devices) {
            $manager->guard($cfg, $vm, $devices);
            return $manager->prepare($cfg, $key, $vm, $uid, $devices, true);
        });
        if ($action === 'dry-run') {
            return $plan;
        }
        proof($manager);
        $manual = $manager->s->run . '/manual-' . $uid . '.json';
        writeJson($manual, (object)['vm' => $vm, 'gpu' => $key, 'expires' => time() + 210]);
        try {
            virsh($manager, 'start', $uid);
        } catch (Refused $error) {
            $pending = $manager->lease($key, $cfg);
            if (($pending->domain ?? null) === $uid) {
                try {
                    recover($manager, $key);
                } catch (\Throwable $recoveryError) {
                    throw new Refused($error->getMessage() . '. Recovery pending: ' . $recoveryError->getMessage());
                }
            }
            throw $error;
        } finally {
            clearstatcache(true, $manual);
            removeFile($manual);
        }
        $journal = $manager->lease($key, $cfg);
        ensure($journal && $journal->domain === $uid && $journal->state === 'VM_ACTIVE'
            && trim(virsh($manager, 'domstate', $uid)) === 'running',
            'Start issued; verified VM_ACTIVE state unavailable. Inspect diagnostics.');
        return ['vm' => $vm, 'result' => 'VM_ACTIVE', 'warnings' => $journal->startup_warnings ?? []];
    });
}

function dispatch(Manager $manager, array $arguments, string $input = ''): mixed
{
    $action = array_shift($arguments);
    $counts = ['discover' => [0], 'status' => [0], 'diagnostics' => [0], 'export' => [0], 'logs' => [0],
        'progress' => [0], 'probe' => [0], 'init' => [0], 'array-stop' => [0], 'array-start' => [0], 'array-starting' => [0],
        'enroll' => [3], 'import' => [0, 2], 'assign' => [1], 'dry-run' => [1], 'start' => [1], 'shutdown' => [1],
        'recover' => [1], 'hook' => [3]];
    ensure(isset($counts[$action]) && in_array(count($arguments), $counts[$action], true), 'Invalid command or arguments');
    if ($action === 'hook') {
        [$vm, $event, $phase] = $arguments;
        if (str_starts_with($vm, 'gsm-probe-') && matches($vm, NAME) && "$event/$phase" === 'prepare/begin') {
            $marker = $manager->s->run . '/' . $vm;
            if (is_file($marker) && readText($marker) === domain($input)[1]) {
                putFile($marker, 'observed');
                throw new Refused('GPU manager hook probe observed');
            }
        }
        $manager->hook($vm, $event, $phase, $input);
        return null;
    }
    if ($action === 'discover') {
        return $manager->hw->discover();
    }
    if ($action === 'probe') {
        return proof($manager);
    }
    if ($action === 'status' || $action === 'diagnostics') {
        return status($manager, $action === 'diagnostics');
    }
    if ($action === 'progress') {
        $result = [];
        foreach ($manager->s->journals() as $path) {
            $contents = @file_get_contents($path);
            if ($contents !== false) {
                $result[basename($path, '.json')] = json_decode($contents, false, 128, JSON_THROW_ON_ERROR);
            }
        }
        return (object)$result;
    }
    if ($action === 'logs') {
        return $manager->s->history();
    }
    if (in_array($action, ['init', 'import', 'enroll'], true)) {
        ensure(strlen($input) <= 262144, 'Configuration too large');
        return configure($manager, $action, $arguments, $input);
    }
    $arrayMarker = $manager->s->run . '/array-stopping';
    if ($action === 'array-stop') {
        putFile($arrayMarker, '');
        return ['stopping' => true];
    }
    if ($action === 'array-starting' || $action === 'array-start') {
        removeFile($arrayMarker);
        if ($action === 'array-starting') {
            return ['stopping' => false];
        }
        return $manager->s->locked('maintenance', function () use ($manager) {
            $result = [];
            foreach ($manager->s->journals() as $path) {
                $key = basename($path, '.json');
                try {
                    $result[$key] = recover($manager, $key);
                } catch (\Throwable $error) {
                    $result[$key] = $error->getMessage();
                    $manager->s->log($key, null, 'DEFERRED', $result[$key]);
                }
            }
            return (object)$result;
        }, true);
    }
    return $manager->s->locked('maintenance', function () use ($manager, $action, $arguments) {
        if ($action === 'recover') {
            return recover($manager, $arguments[0]);
        }
        $cfg = $manager->s->cfg();
        if ($action === 'export') {
            return $cfg;
        }
        $vm = $arguments[0];
        try {
            return vmAction($manager, $cfg, $action, $vm);
        } catch (Refused $error) {
            if ($action === 'start' && isset($cfg->profiles->$vm)) {
                $manager->s->log($cfg->profiles->$vm->gpu, $vm, 'REFUSED', $error->getMessage());
            }
            throw $error;
        }
    }, true);
}

if (PHP_SAPI === 'cli' && realpath($_SERVER['SCRIPT_FILENAME'] ?? '') === __FILE__) {
    try {
        ensure(function_exists('posix_geteuid') && posix_geteuid() === 0, 'Root and PHP POSIX are required');
        $action = $argv[1] ?? '';
        $input = match ($action) {
            'hook' => stream_get_contents(STDIN, 4 * 1024 * 1024 + 1),
            'import' => stream_get_contents(STDIN, 262145),
            default => '',
        };
        $result = dispatch(new Manager(), array_slice($argv, 1), $input);
        if ($action !== 'hook') {
            fwrite(STDOUT, encode($result, true) . "\n");
        }
    } catch (\Throwable $error) {
        fwrite(STDERR, encode(['error' => $error->getMessage()]) . "\n");
        exit(1);
    }
}
