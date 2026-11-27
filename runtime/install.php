<?php
declare(strict_types=1);

namespace Gsm;

require_once __DIR__ . '/controller.php';

const LIB = 'usr/local/lib/gpu-switch-manager';
const WEB = 'usr/local/emhttp/plugins/gpu-switch-manager';
const MANIFEST = LIB . '/installed.json';
const PERSIST = 'boot/config/plugins/gpu-switch-manager';
const TARGET = '/' . LIB . '/qemu-hook';
const PYTHON_CACHE_HASH = 'b56ceb4fc1d8f0c49b7bd4dc91cd176bad7b86a88b9fc509bae6b10b3ee36692';

function digest(string $path): string
{
    $hash = hash_file('sha256', $path);
    ensure($hash !== false, 'Cannot hash ' . $path);
    return $hash;
}

function ownedName(string $name): bool
{
    return (str_starts_with($name, LIB . '/') || str_starts_with($name, WEB . '/'))
        && preg_match('~(?:^|/)(?:\.|\.\.|)(?:/|$)~', $name) === 0;
}

final class Installer
{
    public Store $store;
    private string $hook;
    private string $marker;

    public function __construct(private string $root, private ?string $source, private bool $mounted)
    {
        $this->root = rtrim($root, '/');
        $this->source = $source === null ? null : rtrim($source, '/');
        $this->store = new Store($this->root . '/' . PERSIST, $this->root . '/run/gpu-switch-manager');
        $this->hook = $this->root . HOOK;
        $this->marker = $this->store->config . '/hook-registered';
    }

    private function destination(string $name): string
    {
        return $this->root . '/' . $name;
    }

    private function checkHooks(): void
    {
        ensure(!is_link($this->root . '/etc/libvirt/hooks') && !is_link(dirname($this->hook)), 'Foreign hook directory symlink');
        if (hasPath($this->hook)) {
            $owned = is_link($this->hook) ? readlink($this->hook) === TARGET
                : is_file($this->hook) && is_file($this->marker) && digest($this->hook) === readJson($this->marker);
            ensure($owned, 'Foreign or modified hook occupies ' . HOOK);
        }
        $native = $this->root . '/etc/libvirt/hooks/qemu';
        if (is_file($native)) {
            $contents = readText($native);
            ensure(!str_contains($contents, 'gpu-switch-manager/scripts/vm_')
                && !str_contains($contents, 'libvirt qemu hook for GPU switching'),
                'Legacy main QEMU hook detected; restore the administrator’s original hook first');
        }
    }

    public function register(): bool
    {
        if (!$this->mounted) {
            return false;
        }
        $this->checkHooks();
        $source = $this->destination(LIB . '/qemu-hook');
        $expected = digest($source);
        if (!is_file($this->hook) || is_link($this->hook) || !is_executable($this->hook) || digest($this->hook) !== $expected) {
            putFile($this->hook, readText($source), 0755);
        }
        if (!is_file($this->marker) || readJson($this->marker) !== $expected) {
            writeJson($this->marker, $expected);
        }
        return true;
    }

    private function manifest(string $path): array
    {
        $record = readJson($path);
        ensure($record instanceof \stdClass, 'Invalid package file list');
        $files = (array)$record;
        foreach ($files as $name => $hash) {
            ensure(ownedName((string)$name) && matches($hash, '[0-9a-f]{64}'), 'Invalid package file record');
        }
        $files[MANIFEST] = digest($path);
        return $files;
    }

    private function oldFiles(bool $installed): array
    {
        if ($installed) {
            return array_map(fn($hash) => [$hash], $this->manifest($this->destination(MANIFEST)));
        }
        if ($this->source === null) {
            return [];
        }
        $files = (array)readJson($this->source . '/' . LIB . '/legacy.json');
        foreach ($files as $name => $hashes) {
            $legacyEvent = matches($name, 'usr/local/emhttp/webGui/event_handlers/gpu-switch-(?:array|docker|vm)-(?:start|stop)');
            ensure((ownedName($name) || $legacyEvent) && is_array($hashes) && $hashes, 'Invalid legacy file record');
            foreach ($hashes as $hash) {
                ensure(matches($hash, '[0-9a-f]{64}'), 'Invalid legacy file digest');
            }
        }
        return $files;
    }

    private function checkPreviousWork(bool $legacy, bool $installed, bool $remove): void
    {
        if ($legacy) {
            $pidFile = $this->destination('var/run/gpu-switch-manager/docker-event-monitor.pid');
            if (is_file($pidFile)) {
                $pid = field($pidFile);
                ensure(ctype_digit($pid) && !is_dir($this->destination('proc/' . $pid)),
                    'Inspect and stop the old Docker monitor before migration');
            }
            foreach (paths($this->destination('sys/bus/pci/devices/*/class')) as $class) {
                if (str_starts_with(field($class), '0x03')) {
                    ensure(driver(dirname($class)) !== 'vfio-pci', 'Complete the existing legacy GPU assignment before migration');
                }
            }
        }
        $journals = $this->store->journals();
        if (is_file($this->store->settings)) {
            $cfg = $this->store->cfg();
            if ($remove || $installed) {
                ensure(!$journals, 'Outstanding GPU transaction; finish release or recovery before changing the plugin');
                foreach ($cfg->gpus as $gpu) {
                    if (!$gpu->protected) {
                        foreach ($gpu->functions as $function => $_) {
                            $address = substr($gpu->pci, 0, -1) . $function;
                            ensure(driver($this->destination('sys/bus/pci/devices/' . $address)) !== 'vfio-pci',
                                'An enrolled PCI function still uses VFIO: ' . $address);
                        }
                    }
                }
            }
        } else {
            ensure(!$journals, 'Missing configuration for outstanding transactions');
        }
        if ($remove) {
            ensure($this->mounted || !hasPath($this->marker),
                'Start the array with VM Manager enabled so its hook can be removed from the mounted image');
        }
    }

    private function inspectTrees(array $old, array $new, bool $legacy): array
    {
        foreach ([LIB, WEB] as $tree) {
            $base = $this->destination($tree);
            ensure(!is_link($base), 'Foreign symlink at ' . $tree);
            if (!is_dir($base)) {
                continue;
            }
            foreach (new \RecursiveIteratorIterator(new \RecursiveDirectoryIterator($base, \FilesystemIterator::SKIP_DOTS),
                \RecursiveIteratorIterator::SELF_FIRST) as $entry) {
                $name = substr($entry->getPathname(), strlen($this->root) + 1);
                ensure(!$entry->isLink(), 'Unexpected symlink: ' . $name);
                if ($legacy && $entry->isFile()) {
                    ensure(isset($old[$name]), 'Review unrecognized legacy file: ' . $name);
                }
            }
        }
        $touched = $new;
        foreach ($old as $name => $hashes) {
            $path = $this->destination($name);
            if (hasPath($path)) {
                ensure(!is_link($path) && is_file($path) && in_array(digest($path), $hashes, true),
                    'Review locally modified file: ' . $name);
                $touched[$name] = $new[$name] ?? null;
            }
        }
        foreach ($new as $name => $_) {
            ensure(isset($old[$name]) || !hasPath($this->destination($name)), 'Unowned destination: ' . $name);
        }
        return $touched;
    }

    private function applyFiles(array $touched, bool $remove): void
    {
        $backupRoot = $this->store->run . '/installer-' . uuid();
        $saved = [];
        $hookLink = is_link($this->hook) ? readlink($this->hook) : null;
        $hookText = $hookLink === null && is_file($this->hook) ? readText($this->hook) : null;
        $marker = is_file($this->marker) ? readText($this->marker) : null;
        $hadConfig = is_file($this->store->settings);
        try {
            // Publish PHP dependencies before replacing the old launcher.
            $launchers = array_intersect_key($touched, array_flip([LIB . '/gpuctl', LIB . '/qemu-hook', MANIFEST]));
            $updates = array_filter(array_diff_key($touched, $launchers), fn($hash) => $hash !== null);
            foreach ($updates + $launchers + $touched as $name => $hash) {
                $dest = $this->destination($name);
                $exists = is_file($dest);
                $source = $this->source . '/' . $name;
                $mode = $hash === null ? null : fileperms($source) & 0777;
                if ($hash !== null && $exists && (fileperms($dest) & 0777) === $mode && digest($dest) === $hash) {
                    continue;
                }
                $backup = null;
                if ($exists) {
                    $backup = $backupRoot . '/' . $name;
                    directory(dirname($backup));
                    ensure(copy($dest, $backup) && chmod($backup, fileperms($dest) & 0777), 'Cannot back up ' . $name);
                }
                $saved[$name] = $backup;
                if ($hash === null) {
                    removeFile($dest);
                } else {
                    directory(dirname($dest), 0755);
                    putFile($dest, readText($source), $mode);
                }
            }
            if ($remove) {
                removeFile($this->hook);
                removeFile($this->marker);
            } else {
                if (!$hadConfig) {
                    $this->store->change(defaults());
                }
                $this->register();
            }
        } catch (\Throwable $error) {
            try {
                foreach (array_reverse($saved, true) as $name => $backup) {
                    $dest = $this->destination($name);
                    if ($backup === null) {
                        removeFile($dest);
                    } else {
                        directory(dirname($dest), 0755);
                        putFile($dest, readText($backup), fileperms($backup) & 0777);
                    }
                }
                if ($this->mounted) {
                    if ($hookLink !== null) {
                        removeFile($this->hook);
                        ensure(symlink($hookLink, $this->hook), 'Cannot restore prior hook link');
                    } elseif ($hookText !== null) {
                        putFile($this->hook, $hookText, 0755);
                    } else {
                        removeFile($this->hook);
                    }
                    if ($marker === null) {
                        removeFile($this->marker);
                    } else {
                        putFile($this->marker, $marker);
                    }
                }
                if (!$hadConfig) {
                    removeFile($this->store->settings);
                }
            } catch (\Throwable $rollbackError) {
                throw new Refused($error->getMessage() . '. Rollback failed: ' . $rollbackError->getMessage()
                    . '. Backups retained at ' . $backupRoot);
            }
            $this->removeBackups($backupRoot);
            throw $error;
        }
        $this->removeBackups($backupRoot);
    }

    private function removeBackups(string $root): void
    {
        if (!is_dir($root)) {
            return;
        }
        foreach (new \RecursiveIteratorIterator(new \RecursiveDirectoryIterator($root, \FilesystemIterator::SKIP_DOTS),
            \RecursiveIteratorIterator::CHILD_FIRST) as $entry) {
            $entry->isDir() ? @rmdir($entry->getPathname()) : @unlink($entry->getPathname());
        }
        @rmdir($root);
    }

    public function apply(bool $remove = false): void
    {
        clearstatcache(true);
        ensure(!is_link($this->store->config) && !is_link($this->store->settings)
            && !is_link($this->store->leases) && !is_link($this->marker), 'Unexpected persistent storage symlink');
        $manifest = $this->destination(MANIFEST);
        ensure(!is_link($manifest), 'Unexpected installation manifest symlink');
        $installed = is_file($manifest);
        $old = $this->oldFiles($installed);
        $new = $remove ? [] : $this->manifest($this->source . '/' . MANIFEST);
        foreach ($new as $name => $expected) {
            $path = $this->source . '/' . $name;
            ensure(!is_link($path) && is_file($path) && digest($path) === $expected, 'Package integrity failure: ' . $name);
        }
        $this->checkHooks();
        $legacy = !$installed && (bool)array_filter(array_keys($old), fn($name) => hasPath($this->destination($name)));
        $this->checkPreviousWork($legacy, $installed, $remove);
        $touched = $this->inspectTrees($old, $new, $legacy);
        $this->applyFiles($touched, $remove);
        foreach (array_keys($touched) as $name) {
            $parent = dirname($name);
            while ($parent === LIB || $parent === WEB || ownedName($parent)) {
                @rmdir($this->destination($parent));
                $parent = dirname($parent);
            }
        }
        if ($legacy) {
            foreach (['javascript', 'styles', 'templates'] as $directory) {
                @rmdir($this->destination(WEB . '/' . $directory));
            }
        }
        $cache = $this->store->config . '/python-runtime.tar.gz';
        if (!$remove && !is_link($cache) && is_file($cache) && digest($cache) === PYTHON_CACHE_HASH) {
            if (!@unlink($cache)) {
                fwrite(STDERR, "Could not remove the obsolete private Python archive at $cache\n");
            }
        }
        if ($remove) {
            foreach (paths($this->store->run . '/*') as $path) {
                if (matches(basename($path), '(?:history\.json|history\.lock|maintenance\.lock|probe\.lock|array-stopping|(?:gpu-' . NAME
                    . '|definition-' . DOMAIN_UUID . ')\.lock|manual-' . DOMAIN_UUID . '\.json)')) {
                    removeFile($path);
                }
            }
            @rmdir($this->store->run);
        }
    }
}

if (PHP_SAPI === 'cli' && realpath($_SERVER['SCRIPT_FILENAME'] ?? '') === __FILE__) {
    try {
        ensure(function_exists('posix_geteuid') && posix_geteuid() === 0 && count($argv) === 2, 'Root and an installer operation are required');
        $action = $argv[1];
        ensure(in_array($action, ['install', 'register', 'remove'], true), 'Invalid installer operation');
        $mounted = false;
        try {
            command(['mountpoint', '-q', '/etc/libvirt'], 4);
            $mounted = true;
        } catch (Refused) {
            // Unraid mounts libvirt.img when VM Manager starts.
        }
        if ($action === 'install') {
            $dependencies = dependencies();
            foreach (['docker', 'virsh', 'lspci', 'timeout', 'mountpoint', 'simplexml', 'dom', 'posix'] as $dependency) {
                ensure((bool)$dependencies[$dependency], 'Missing required dependency: ' . $dependency);
            }
            $version = trim(command(['virsh', '--version'], 5));
            ensure(version_compare($version, '6.5.0', '>='), 'libvirt 6.5 or later required');
            ensure(!paths('/boot/config/plugins/gpu-switch-manager-*.plg'), 'Archive old version-named manifests before installation');
        }
        $installer = new Installer('/', $action === 'install' ? dirname(__DIR__, 4) : null, $mounted);
        $installer->store->locked('maintenance', function () use ($action, $installer, $mounted) {
            if ($action === 'register') {
                ensure($installer->register(), 'Libvirt image is not mounted');
                return;
            }
            $installer->apply($action === 'remove');
            if ($action === 'install') {
                $version = readJson(__DIR__ . '/release.json')->version;
                $registration = $mounted ? 'hook registered' : 'hook registration deferred until the libvirt image mounts';
                fwrite(STDOUT, "GPU Switch Manager $version installed ($registration)\n");
            }
        });
    } catch (\Throwable $error) {
        fwrite(STDERR, $error->getMessage() . "\n");
        exit(1);
    }
}
