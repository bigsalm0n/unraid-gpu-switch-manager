<?php
declare(strict_types=1);
header('Content-Type: application/json');
header('Cache-Control: no-store');
function failRequest(int $status, string $message): void {
    http_response_code($status);
    echo json_encode(['error' => $message]);
    exit;
}
if (!isset($_COOKIE[session_name()]) || !session_start(['read_and_close' => true]) ||
    empty($_SESSION['unraid_login']) || ($_SESSION['unraid_user'] ?? '') !== 'root') {
    failRequest(403, 'Sign in as the Unraid administrator');
}
if ($_SERVER['REQUEST_METHOD'] !== 'POST') failRequest(405, 'POST required');
$vars = @parse_ini_file('/var/local/emhttp/var.ini', false, INI_SCANNER_RAW);
$token = $vars['csrf_token'] ?? '';
if ($token === '' || !is_string($_POST['csrf_token'] ?? null) || !hash_equals($token, $_POST['csrf_token'])) {
    failRequest(403, 'Reload the Unraid page to renew the CSRF token');
}
$action = $_POST['action'] ?? '';
$commands = ['status' => [], 'diagnostics' => [], 'logs' => [], 'progress' => [], 'export' => [],
             'import' => [], 'enroll' => ['key', 'pci', 'role'], 'dry-run' => ['vm'],
             'assign' => ['vm'], 'start' => ['vm'], 'shutdown' => ['vm'], 'recover' => ['gpu']];
if (!is_string($action) || !isset($commands[$action])) failRequest(400, 'Unknown operation');
if (in_array($action, ['import', 'enroll', 'assign', 'start', 'shutdown', 'recover'], true) && ($_POST['confirmed'] ?? '') !== 'yes') {
    failRequest(400, 'Confirmation required');
}
$args = ['/usr/local/lib/gpu-switch-manager/gpuctl', $action];
foreach ($commands[$action] as $field) {
    $value = $_POST[$field] ?? null;
    if (!is_string($value) || !preg_match('/^[a-zA-Z0-9][a-zA-Z0-9 _.():-]{0,127}$/D', $value)) failRequest(400, "Invalid $field");
    $args[] = $value;
}
$input = '';
if ($action === 'import') {
    $input = $_POST['config'] ?? null;
    $revision = $_POST['revision'] ?? null;
    if (!is_string($input) || strlen($input) > 262144 || !is_string($revision) || !preg_match('/^[0-9a-f]{64}$/D', $revision)) {
        failRequest(400, 'Invalid configuration or revision');
    }
    array_push($args, '--revision', $revision);
}
set_time_limit(240);
$proc = proc_open($args, [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
if (!is_resource($proc)) failRequest(503, 'Backend unavailable');
fwrite($pipes[0], $input);
fclose($pipes[0]);
$output = stream_get_contents($pipes[1]);
fclose($pipes[1]);
$error = stream_get_contents($pipes[2]);
fclose($pipes[2]);
if (proc_close($proc) !== 0) failRequest(409, json_decode($error, true)['error'] ?? 'Backend failed. Inspect diagnostics.');
if (json_decode($output, true) === null) failRequest(502, 'Invalid backend response');
echo $output;
