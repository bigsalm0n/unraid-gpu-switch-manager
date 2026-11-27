<section id="gsm">
  <p id="gsm-message" role="status" aria-live="polite">Reading hardware and profiles…</p>
  <button type="button" id="gsm-refresh">Discover GPUs</button>
  <label><input type="checkbox" id="gsm-live" checked> Refresh dashboard every 15 seconds</label>
  <button type="button" id="gsm-diag">Diagnostics</button>
  <button type="button" id="gsm-logs">Operation log</button>
  <div id="gsm-devices"></div>
  <h3>GPU enrollment</h3>
  <label>Device <select id="gsm-pci"></select></label>
  <label>Profile key <input id="gsm-key" maxlength="128" placeholder="gaming"></label>
  <label>Role <select id="gsm-role"><option>protected</option><option>shared</option></select></label>
  <button type="button" id="gsm-enroll">Save device identity</button>
  <h3>VM sharing profiles</h3>
  <label><input id="gsm-enabled" type="checkbox"> Enable automatic switching</label>
  <button type="button" id="gsm-enable">Save switching setting</button>
  <p><label>VM <select id="gsm-vm"></select></label>
  <label>Shared GPU <select id="gsm-gpu"></select></label></p>
  <div id="gsm-workloads"></div>
  <button type="button" id="gsm-save">Save profile</button>
  <button type="button" id="gsm-delete">Delete profile</button>
  <button type="button" id="gsm-assign">Add managed devices to VM</button>
  <p><button type="button" id="gsm-dry">Dry run</button>
  <button type="button" id="gsm-start">Start VM</button>
  <button type="button" id="gsm-shutdown">Request guest shutdown</button>
  <button type="button" id="gsm-recover">Recover selected GPU</button></p>
  <details><summary>Configuration import and export</summary>
    <button type="button" id="gsm-export">Download configuration</button>
    <label>Import file <input type="file" id="gsm-file" accept="application/json,.json"></label>
    <button type="button" id="gsm-import">Import configuration</button>
  </details>
  <pre id="gsm-detail" aria-live="polite" style="white-space:pre-wrap"></pre>
</section>
<script src="/plugins/gpu-switch-manager/dashboard.js"></script>
