<?php
declare(strict_types=1);

namespace Gsm;

require_once __DIR__ . '/hardware.php';

function domain(string $input): array
{
    $root = xml($input);
    ensure($root->getName() === 'domain', 'Expected libvirt domain XML');
    $devices = [];
    foreach ($root->xpath('/domain/devices/hostdev[@type="pci"] | /domain/devices/interface[@type="hostdev"]') as $dev) {
        $address = $dev->source->address;
        ensure(count($address) === 1, 'Missing source PCI address');
        $parts = [];
        foreach (['domain', 'bus', 'slot', 'function'] as $part) {
            $value = (string)($address[$part] ?? '0');
            ensure(matches($value, '(?:0[xX])?[0-9a-fA-F]{1,4}'), 'Invalid source PCI address');
            $parts[] = intval(preg_replace('/^0x/i', '', $value), 16);
        }
        $pci = sprintf('%04x:%02x:%02x.%x', ...$parts);
        ensure(matches($pci, PCI) && !isset($devices[$pci]), 'Invalid or duplicate PCI hostdev');
        $devices[$pci] = $dev->getName() === 'hostdev' && (string)$dev['managed'] === 'yes'
            && (string)($dev->driver['name'] ?? 'vfio') === 'vfio';
    }
    ksort($devices);
    return [(string)$root->name, (string)$root->uuid, $devices];
}

function running(object $container): bool
{
    return $container->State->Running;
}

final class Manager
{
    public function __construct(public Store $s = new Store(), public Hardware $hw = new Hardware()) {}

    public function record(string $key, object $journal, string $state, string $message): void
    {
        $journal->state = $state;
        $journal->message = $message;
        writeJson($this->s->journal($key), $journal);
        $this->s->log($key, $journal->vm, $state, $message);
    }

    public function lease(string $key, object $cfg): ?object
    {
        $path = $this->s->journal($key);
        clearstatcache(true, $path);
        if (!hasPath($path)) {
            return null;
        }
        $journal = readJson($path);
        ensure($journal instanceof \stdClass && isset($cfg->gpus->$key, $journal->gpu)
            && same($journal->gpu, $cfg->gpus->$key) && ($journal->key ?? null) === $key,
            'Journal identity mismatch; inspect transaction');
        ensure(matches($journal->vm ?? null, VM) && ($cfg->profiles->{$journal->vm}->gpu ?? null) === $key,
            'Journal profile mismatch');
        ensure(matches($journal->domain ?? null, DOMAIN_UUID) && matches($journal->transaction ?? null, DOMAIN_UUID),
            'Invalid transaction identity');
        $expected = array_map(fn($number) => substr($journal->gpu->pci, 0, -1) . $number,
            array_keys((array)$journal->gpu->functions));
        $drivers = (array)($journal->drivers ?? null);
        ensure(count($drivers) === count($expected) && !array_diff($expected, array_keys($drivers)), 'Invalid journal PCI functions');
        foreach ($drivers as $address => $driver) {
            $function = substr($address, -1);
            $unboundAudio = $driver === null && $address !== $journal->gpu->pci
                && str_starts_with($journal->gpu->functions->$function, '0403');
            ensure($unboundAudio || (is_string($driver) && $driver !== '' && $driver !== 'vfio-pci'), 'Invalid original drivers');
        }
        ensure($drivers[$journal->gpu->pci] === 'nvidia', 'Invalid original graphics driver');
        ensure(in_array($journal->state ?? null, ['PREPARING_VM', 'VFIO_BOUND', 'VM_ACTIVE', 'RESTORING_HOST', 'ERROR'], true),
            'Invalid journal state');
        $items = ordered($cfg->profiles->{$journal->vm}->containers);
        ensure(is_array($journal->containers ?? null) && count($journal->containers) === count($items), 'Incomplete container snapshot');
        foreach ($items as $index => $item) {
            $row = $journal->containers[$index];
            ensure($row instanceof \stdClass && ($row->name ?? null) === $item->name && ($row->policy ?? null) === $item->policy
                && matches($row->id ?? null, '[0-9a-f]{64}') && is_bool($row->running ?? null)
                && is_bool($row->attempted ?? null) && ($row->selectors ?? null) instanceof \stdClass
                && ($row->policy !== 'start' || is_bool($row->vm_start_attempted ?? null)),
                'Invalid container snapshot');
        }
        return $journal;
    }

    public function target(object $cfg, string $key): array
    {
        $gpu = $cfg->gpus->$key;
        foreach ($cfg->gpus as $other) {
            if ($other->protected && same($other->functions, $gpu->functions)) {
                ensure($other->uuid && strcasecmp($other->uuid, $gpu->uuid) !== 0,
                    'Indistinguishable protected GPU; confirm its persistent identity');
            }
        }
        return $this->hw->verify($gpu);
    }

    private function plan(object $cfg, string $key, object $profile, array $devices): array
    {
        $data = $this->target($cfg, $key);
        ensure($devices === array_fill_keys(array_keys($data['drivers']), true),
            'VM must have managed VFIO hostdev entries for exactly the approved PCI functions');
        $this->checkArray();
        $containers = $this->hw->containers();
        $items = ordered($profile->containers);
        $configured = array_column($items, null, 'name');
        ensure(!array_diff_key($configured, $containers), 'Configured container is missing');
        $clients = $this->hw->clients($data);
        $found = $this->hw->assignments($data, $containers, $clients);
        $unknown = array_filter(array_diff_key($found, $configured), fn($name) => running($containers[$name]), ARRAY_FILTER_USE_KEY);
        ensure(!$unknown, 'Set an explicit policy for potential GPU containers: ' . implode(', ', array_keys($unknown)));
        $candidates = array_filter($configured, fn($item) => in_array($item->policy, ['restore', 'stop'], true) && running($containers[$item->name]));
        $stopping = array_intersect_key($candidates, $found);
        $candidatePids = null;
        $ownerOf = function (int $pid) use ($containers, $candidates, &$candidatePids): ?string {
            $owner = $this->hw->owner($pid, $containers);
            if ($owner !== null) {
                return $owner;
            }
            if ($candidatePids === null) {
                $candidatePids = [];
                foreach ($candidates as $name => $_) {
                    foreach ($this->hw->processIds($containers[$name]) as $containerPid) {
                        $candidatePids[$containerPid] = (string)$name;
                    }
                }
            }
            return $candidatePids[$pid] ?? null;
        };
        foreach ($clients as $pid) {
            $owner = $ownerOf($pid);
            ensure($owner !== null && isset($candidates[$owner]), 'Target has an unquiesced client PID ' . $pid);
            $stopping[$owner] = $candidates[$owner];
        }
        foreach ($items as $item) {
            if (isset($candidates[$item->name]) && array_intersect($item->after, array_keys($stopping))) {
                $stopping[$item->name] = $item;
            }
        }
        if ($stopping) {
            foreach ($this->hw->discover() as $other) {
                if ($other['pci'] === $data['pci'] || in_array($other['drivers'][$other['pci']], [null, 'vfio-pci'], true)) {
                    continue;
                }
                ensure(!$other['error'], 'Cannot check clients on another GPU');
                foreach ($this->hw->clients($other) as $pid) {
                    $owner = $ownerOf($pid);
                    ensure($owner === null || !isset($stopping[$owner]), 'A selected container is active on another GPU at ' . $other['pci']);
                }
            }
        }
        $rows = [];
        foreach ($items as $item) {
            $container = $containers[$item->name];
            $willStop = isset($stopping[$item->name]);
            $willStart = $item->policy === 'start' && !running($container);
            if ($item->policy === 'start') {
                $this->hw->verifyStartupContainer($container);
                if ($willStart) {
                    foreach ($item->after as $dependency) {
                        ensure(!isset($stopping[$dependency]) && (running($containers[$dependency])
                            || $configured[$dependency]->policy === 'start'), 'VM-start dependency will not be running: ' . $dependency);
                    }
                }
            }
            if ($willStop) {
                ensure(empty($container->State->Paused) && empty($container->State->Restarting)
                    && empty($container->HostConfig->AutoRemove), 'Paused, restarting or auto-remove container: ' . $item->name);
            }
            $rows[] = (object)['name' => $item->name, 'policy' => $item->policy, 'id' => $container->Id,
                'running' => running($container), 'attempted' => false, 'will_stop' => $willStop,
                'will_start' => $willStart, 'vm_start_attempted' => false,
                'selectors' => $item->policy === 'restore' && $willStop ? $this->hw->selectors($container) : (object)[]];
        }
        return [$data, $rows];
    }

    private function checkArray(): void
    {
        $marker = $this->s->run . '/array-stopping';
        clearstatcache(true, $marker);
        ensure(!file_exists($marker), 'Array is stopping');
    }

    private function manualStartAllowed(string $uid, string $vm, string $key): bool
    {
        $path = $this->s->run . '/manual-' . $uid . '.json';
        if (!is_file($path)) {
            return false;
        }
        $request = readJson($path);
        if (($request->vm ?? null) !== $vm || ($request->gpu ?? null) !== $key || ($request->expires ?? 0) < time()) {
            return false;
        }
        removeFile($path);
        return true;
    }

    public function prepare(object $cfg, string $key, string $vm, string $uid, array $devices, bool $dry = false): ?array
    {
        ensure(matches($uid, DOMAIN_UUID), 'Invalid domain UUID');
        $profile = $cfg->profiles->$vm;
        ensure($dry || $this->manualStartAllowed($uid, $vm, $key) || ($cfg->enabled && ($profile->automatic ?? true)),
            'Automatic sharing is disabled. Start this VM from GPU Switch Manager.');
        $existing = $this->lease($key, $cfg);
        ensure(!$existing, 'GPU reserved by ' . ($existing->vm ?? '') . '; inspect outstanding transaction');
        [$data, $rows] = $this->plan($cfg, $key, $profile, $devices);
        if ($dry) {
            return ['gpu' => $key, 'vm' => $vm, 'drivers' => $data['drivers'], 'containers' => $rows];
        }
        $journal = (object)['transaction' => uuid(), 'gpu' => $cfg->gpus->$key, 'key' => $key, 'vm' => $vm,
            'domain' => $uid, 'drivers' => (object)$data['drivers'], 'containers' => $rows];
        $this->record($key, $journal, 'PREPARING_VM', 'Validated host and captured workload state');
        $deadline = hrtime(true) + 90_000_000_000;
        try {
            foreach (array_reverse($rows) as $row) {
                if ($row->will_stop) {
                    $this->checkArray();
                    ensure(hrtime(true) < $deadline - 22_000_000_000, 'Workload transition timed out');
                    $this->checkContainer($row, true);
                    $row->attempted = true;
                    writeJson($this->s->journal($key), $journal);
                    $this->hw->run(['docker', 'stop', '--time', '15', $row->id], 22);
                    $this->checkContainer($row, false);
                }
            }
            $data = $this->hw->verify($journal->gpu);
            ensure(same((object)$data['drivers'], $journal->drivers), 'Host PCI bindings changed during preparation');
            ensure(!$this->hw->clients($data), 'Target device handles remain open');
            $this->checkArray();
            $this->s->log($key, $vm, 'PREPARING_VM', 'Target idle; allowing managed PCI assignment');
        } catch (\Throwable $error) {
            $this->record($key, $journal, 'ERROR', $error->getMessage());
            $this->restore($key, $journal);
            throw $error;
        }
        return null;
    }

    private function checkContainer(object $row, ?bool $expected = null): object
    {
        $container = $this->hw->containers()[$row->name] ?? null;
        ensure($container && $container->Id === $row->id, 'Container removed or replaced: ' . $row->name);
        if ($expected !== null) {
            ensure(running($container) === $expected, 'Unexpected running state for ' . $row->name);
        }
        return $container;
    }

    private function startVmWorkloads(string $key, object $journal, object $profile): void
    {
        $rows = array_column($journal->containers, null, 'name');
        $deadline = hrtime(true) + 45_000_000_000;
        $warnings = [];
        foreach (ordered($profile->containers) as $item) {
            $row = $rows[$item->name];
            if ($item->policy !== 'start' || $row->running || $row->vm_start_attempted) {
                continue;
            }
            if (hrtime(true) >= $deadline - 12_000_000_000) {
                $warnings[] = 'VM-start workload time limit reached at ' . $row->name;
                break;
            }
            try {
                $this->checkArray();
                $container = $this->checkContainer($row);
                $this->hw->verifyStartupContainer($container);
                if (running($container)) {
                    continue;
                }
                foreach ($item->after as $dependency) {
                    $this->checkContainer($rows[$dependency], true);
                }
                ensure(hrtime(true) < $deadline - 12_000_000_000, 'Workload startup timed out');
                $row->vm_start_attempted = true;
                writeJson($this->s->journal($key), $journal);
                $this->hw->run(['docker', 'start', $row->id], 12);
                $this->checkContainer($row, true);
                $this->s->log($key, $journal->vm, 'VM_ACTIVE', 'Started VM workload: ' . $row->name);
            } catch (\Throwable $error) {
                $warnings[] = $row->name . ': ' . $error->getMessage();
            }
        }
        if ($warnings) {
            $journal->startup_warnings = $warnings;
            $this->record($key, $journal, 'VM_ACTIVE', 'VM running. Container startup failed: ' . implode('; ', $warnings));
        }
    }

    public function restore(string $key, object $journal): void
    {
        $this->record($key, $journal, 'RESTORING_HOST', 'Checking original drivers and userspace');
        try {
            $end = hrtime(true) + 12_000_000_000;
            while (true) {
                try {
                    $data = $this->hw->verify($journal->gpu);
                    ensure(same((object)$data['drivers'], $journal->drivers), 'Original drivers have not returned');
                    break;
                } catch (Refused $error) {
                    if (hrtime(true) >= $end) {
                        throw $error;
                    }
                    usleep(400_000);
                }
            }
            $this->checkArray();
            $profile = $this->s->cfg()->profiles->{$journal->vm};
            $policies = array_column($profile->containers, null, 'name');
            $rows = array_column($journal->containers, null, 'name');
            $deadline = hrtime(true) + 90_000_000_000;
            foreach ($journal->containers as $row) {
                if ($row->running && $row->attempted && $row->policy === 'restore') {
                    $container = $this->checkContainer($row);
                    ensure(same($this->hw->selectors($container), $row->selectors), 'Docker GPU mapping changed for ' . $row->name);
                    if (!running($container)) {
                        foreach ($policies[$row->name]->after as $dependency) {
                            $this->checkContainer($rows[$dependency], true);
                        }
                        $this->checkArray();
                        ensure(hrtime(true) < $deadline - 30_000_000_000, 'Workload restoration timed out');
                        $this->hw->run(['docker', 'start', $row->id], 30);
                        $this->checkContainer($row, true);
                    }
                }
            }
            $this->s->log($key, $journal->vm, 'HOST_AVAILABLE', 'Verified original drivers, UUID and configured restoration');
            removeFile($this->s->journal($key));
            syncDirectory($this->s->leases);
        } catch (\Throwable $error) {
            $this->record($key, $journal, 'ERROR', $error->getMessage() . '. Inspect drivers, domain and container IDs before recovery.');
            throw $error;
        }
    }

    public function guard(object $cfg, string $vm, array $devices): void
    {
        foreach ($devices as $address => $_) {
            $slot = $this->hw->pci . '/' . $address;
            // Refuse an enrolled slot even when its group link is missing.
            $members = array_unique([$address, ...basenames(paths($slot . '/iommu_group/devices/*'))]);
            $slots = array_unique(array_map(fn($member) => substr($member, 0, -1), $members));
            foreach ($cfg->gpus as $key => $gpu) {
                if (in_array(substr($gpu->pci, 0, -1), $slots, true)) {
                    ensure(!$gpu->protected && ($cfg->profiles->$vm->gpu ?? null) === $key,
                        "PCI device $address overlaps reserved GPU $key");
                }
            }
            if (!isset($cfg->profiles->$vm)) {
                foreach ($slots as $peer) {
                    foreach (paths($this->hw->pci . '/' . $peer . '?/class') as $class) {
                        if (str_starts_with(field($class), '0x03')) {
                            $data = $this->hw->info(basename(dirname($class)));
                            foreach ($cfg->gpus as $key => $gpu) {
                                $resembles = $gpu->uuid && $data['uuid'] ? strcasecmp($gpu->uuid, $data['uuid']) === 0
                                    : same($gpu->functions, $data['functions']);
                                ensure(!$resembles, 'GPU resembles enrolled hardware; confirm profile or remap: ' . $key);
                            }
                        }
                    }
                }
            }
        }
    }

    public function hook(string $vm, string $event, string $phase, string $input): void
    {
        if (!in_array("$event/$phase", ['prepare/begin', 'start/begin', 'started/begin', 'release/end'], true)) {
            return;
        }
        [$name, $uid, $devices] = domain($input);
        ensure($name === $vm, 'Hook VM name differs from XML');
        if (!$devices) {
            try {
                if (!isset($this->s->cfg()->profiles->$vm)) {
                    return;
                }
            } catch (\Throwable) {
                return;
            }
        }
        $this->s->locked('maintenance', function () use ($event, $vm, $uid, $devices) {
            $cfg = $this->s->cfg();
            if ($event === 'prepare') {
                $this->guard($cfg, $vm, $devices);
            }
            if (!isset($cfg->profiles->$vm)) {
                return;
            }
            $key = $cfg->profiles->$vm->gpu;
            $this->s->locked('gpu-' . $key, function () use ($cfg, $key, $vm, $uid, $event, $devices) {
                if ($event === 'prepare') {
                    try {
                        $this->prepare($cfg, $key, $vm, $uid, $devices);
                    } catch (\Throwable $error) {
                        $this->s->log($key, $vm, 'REFUSED', $error->getMessage());
                        throw $error;
                    }
                    return;
                }
                $journal = $this->lease($key, $cfg);
                if (!$journal || $journal->vm !== $vm || $journal->domain !== $uid) {
                    ensure($event === 'release', 'Startup has no matching prepared transaction');
                    return;
                }
                if ($event === 'release') {
                    $this->restore($key, $journal);
                    return;
                }
                try {
                    $data = $this->hw->verify($journal->gpu, false);
                    ensure($devices === array_fill_keys(array_keys($data['drivers']), true), 'Hostdev configuration changed during startup');
                    ensure(!array_diff($data['drivers'], ['vfio-pci']), 'Guest started without expected VFIO drivers');
                    if ($event === 'start') {
                        $this->record($key, $journal, 'VFIO_BOUND', 'All approved functions use VFIO before guest launch');
                    } else {
                        if ($journal->state !== 'VM_ACTIVE') {
                            $this->record($key, $journal, 'VM_ACTIVE', 'Libvirt started guest and all functions use VFIO');
                            $this->startVmWorkloads($key, $journal, $cfg->profiles->$vm);
                        }
                    }
                } catch (\Throwable $error) {
                    $this->record($key, $journal, 'ERROR', $error->getMessage());
                    throw $error;
                }
            });
        }, true);
    }
}
