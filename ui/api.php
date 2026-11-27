<?php
declare(strict_types=1);
header('Content-Type: application/json');
header('Cache-Control: no-store');
function failRequest(int $status, string $message): void {
    http_response_code($status);
    echo json_encode(['error' => $message]);
    exit;
}
if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') failRequest(405, 'POST required');
// WebGUI's auto_prepend_file validates and consumes csrf_token before routing.
if (!function_exists('csrf_terminate')) failRequest(503, 'Unraid request validation is unavailable');
if (!isset($_COOKIE[session_name()]) || !session_start(['read_and_close' => true]) ||
    empty($_SESSION['unraid_login']) || ($_SESSION['unraid_user'] ?? '') !== 'root') {
    failRequest(401, 'Sign in as the Unraid administrator, then reopen GPU Switch Manager');
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
$stdin = tmpfile();
$stderr = tmpfile();
if (!$stdin || !$stderr || fwrite($stdin, $input) !== strlen($input) || !rewind($stdin)) {
    failRequest(503, 'Unable to prepare the backend request');
}
if (!is_executable($args[0])) failRequest(503, 'Backend executable is missing. Check plugin installation.');
$proc = proc_open($args, [0 => $stdin, 1 => ['pipe', 'w'], 2 => $stderr], $pipes);
if (!is_resource($proc)) failRequest(503, 'Backend unavailable');
$output = stream_get_contents($pipes[1]);
fclose($pipes[1]);
$exit = proc_close($proc);
rewind($stderr);
$error = stream_get_contents($stderr, 65536);
fclose($stdin);
fclose($stderr);
if ($exit !== 0) {
    $reason = json_decode($error, true)['error'] ?? null;
    failRequest(is_string($reason) ? 409 : 502, is_string($reason) ? $reason : 'Backend execution failed. Check the runtime dependencies and system log.');
}
if (json_decode($output, true) === null) failRequest(502, 'Invalid backend response');
echo $output;
