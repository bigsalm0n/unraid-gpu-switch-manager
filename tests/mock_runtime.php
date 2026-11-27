<?php
declare(strict_types=1);

namespace Gsm;

$repo = dirname(__DIR__);
require_once $repo . '/runtime/controller.php';
require_once $repo . '/runtime/install.php';

function send(array $message): void
{
    fwrite(STDOUT, encode($message) . "\n");
    fflush(STDOUT);
}

try {
    $request = json_decode(fgets(STDIN), false, 128, JSON_THROW_ON_ERROR);
    $runner = function (array $args, int $timeout) {
        send(['command' => $args, 'timeout' => $timeout]);
        $reply = json_decode(fgets(STDIN), false, 128, JSON_THROW_ON_ERROR);
        if (isset($reply->error)) {
            throw new Refused($reply->error);
        }
        return $reply->output;
    };
    if ($request->target === 'installer') {
        $installer = new Installer($request->root, $request->source ?? null, $request->mounted);
        $result = $installer->store->locked('maintenance', fn() => $installer->apply($request->remove));
    } else {
        $root = $request->root;
        $store = new Store("$root/flash", "$root/ram");
        $hardware = new Hardware("$root/sys", "$root/proc", "$root/dev", $runner);
        $manager = new Manager($store, $hardware);
        $args = $request->args;
        if ($request->target === 'hardware' && in_array($request->method, ['clients', 'assignments'], true)) {
            $args[0] = (array)$args[0];
            $args[0]['drivers'] = (array)$args[0]['drivers'];
            $args[0]['processes'] = array_map(fn($process) => (array)$process, $args[0]['processes']);
            if ($request->method === 'assignments') {
                $args[1] = (array)$args[1];
            }
        } elseif ($request->target === 'manager' && $request->method === 'prepare') {
            $args[4] = (array)$args[4];
        }
        $result = match ($request->target) {
            'dispatch' => dispatch($manager, $args[0], $args[1] ?? ''),
            'manager' => $manager->{$request->method}(...$args),
            'hardware' => $hardware->{$request->method}(...$args),
            'store' => $store->{$request->method}(...$args),
            'validate' => validate($args[0]),
            'defaults' => defaults(),
            'domain' => domain($args[0]),
            default => throw new \LogicException('Unknown fixture action'),
        };
    }
    send(['result' => $result]);
} catch (\Throwable $error) {
    send(['error' => $error->getMessage(), 'type' => get_class($error)]);
}
