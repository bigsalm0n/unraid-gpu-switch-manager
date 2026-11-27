<link rel="stylesheet" href="/plugins/gpu-switch-manager/dashboard.css?v=<?= hash_file("sha256", __DIR__ . "/dashboard.css") ?>">
<section id="gsm" data-csrf="<?= htmlspecialchars((string)($var['csrf_token'] ?? ''), ENT_QUOTES, 'UTF-8') ?>">
  <p id="gsm-message" role="status" aria-live="polite">Reading hardware and profiles…</p>
  <h3>GPU overview</h3>
  <button type="button" id="gsm-refresh">Refresh</button>
  <label><input type="checkbox" id="gsm-live" checked> Refresh dashboard every 15 seconds</label>
  <div id="gsm-devices"></div>
  <h3>GPU configuration</h3>
  <div class="gsm-fields">
  <label>Device <select id="gsm-pci"></select></label>
  <label>Profile key <input id="gsm-key" maxlength="128" placeholder="gaming"></label>
  <label>Role <select id="gsm-role"><option>protected</option><option>shared</option></select></label>
  <button type="button" id="gsm-enroll">Save device identity</button>
  <button type="button" id="gsm-unmanage">Remove enrollment</button>
  </div>
  <h3>VM sharing profiles</h3>
  <label><input id="gsm-enabled" type="checkbox"> Enable automatic switching</label>
  <button type="button" id="gsm-enable">Save switching setting</button>
  <div class="gsm-fields"><label>VM profile <select id="gsm-vm"></select></label>
  <button type="button" id="gsm-load-profile">Reload saved profile</button>
  <label>Shared GPU <select id="gsm-gpu"></select></label>
  <label><input id="gsm-automatic" type="checkbox"> Switch when started from VM Manager</label></div>
  <p id="gsm-trigger"></p>
  <p id="gsm-workload-summary"></p>
  <div class="gsm-fields">
  <label>Start a CPU-only container with this VM <select id="gsm-start-container"></select></label>
  <button type="button" id="gsm-add-start-container">Add startup container</button>
  </div>
  <label><input id="gsm-show-other" type="checkbox"> Show possible GPU access and other containers</label>
  <div id="gsm-workloads"></div>
  <button type="button" id="gsm-save">Save profile</button>
  <button type="button" id="gsm-delete">Delete profile</button>
  <button type="button" id="gsm-assign">Add managed devices to VM</button>
  <p><button type="button" id="gsm-dry">Validate and dry run</button>
  <button type="button" id="gsm-start">Start VM</button>
  <button type="button" id="gsm-shutdown">Request guest shutdown</button>
  <button type="button" id="gsm-recover">Recover selected GPU</button></p>
  <p id="gsm-vm-message" role="status" aria-live="polite"></p>
  <details><summary>Configuration import and export</summary>
    <button type="button" id="gsm-export">Download configuration</button>
    <label>Import file <input type="file" id="gsm-file" accept="application/json,.json"></label>
    <button type="button" id="gsm-import">Import configuration</button>
  </details>
  <h3>Diagnostics</h3>
  <button type="button" id="gsm-diag">View diagnostics</button>
  <button type="button" id="gsm-logs">Operation log</button>
  <button type="button" id="gsm-download">Download diagnostics</button>
  <pre id="gsm-detail" aria-live="polite"></pre>
</section>
<script defer src="/plugins/gpu-switch-manager/dashboard.js?v=<?= hash_file("sha256", __DIR__ . "/dashboard.js") ?>"></script>
