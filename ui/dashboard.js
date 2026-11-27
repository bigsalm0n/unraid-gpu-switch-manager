(() => {
    "use strict";
    const root = document.getElementById("gsm");
    if (!root) return;
    const el = id => document.getElementById("gsm-" + id);
    let state, polling, busy = false;
    function syncControls() {
        for (const control of root.querySelectorAll("button, select, input")) {
            control.disabled = busy || (!state && !["gsm-refresh", "gsm-diag", "gsm-logs", "gsm-download"].includes(control.id));
        }
        if (!state || busy) return;
        for (const id of ["save", "delete", "assign", "dry", "start", "shutdown"])
            el(id).disabled = !el("vm").value || (id === "save" && !el("gpu").value);
        el("load-profile").disabled = !Object.hasOwn(state.config.profiles, el("vm").value);
        el("add-start-container").disabled = !el("vm").value || !el("gpu").value || !el("start-container").value;
        el("recover").disabled = !el("gpu").value;
        el("enroll").disabled = !el("pci").value;
        el("unmanage").disabled = !state.devices.find(d => d.pci === el("pci").value)?.key;
    }
    async function api(action, fields = {}) {
        const token = root.dataset.csrf;
        if (!token) throw Error("Unraid did not provide a request token. Reopen this page from Settings.");
        const response = await fetch("/plugins/gpu-switch-manager/api.php", {
            method: "POST", credentials: "same-origin", cache: "no-store", redirect: "manual",
            headers: {Accept: "application/json"},
            body: new URLSearchParams({...fields, action, csrf_token: token})
        });
        if (response.type === "opaqueredirect" || response.status === 401)
            throw Error("Your Unraid session requires sign-in. Sign in and reopen this page.");
        const result = await response.json().catch(() => null);
        if (!response.ok) {
            if (response.status === 403 && /^(wrong|missing|uninitialized) csrf_token$/.test(result?.error))
                throw Error("Unraid rejected the request token. Reopen this page and try again.");
            throw Error(result?.error || (response.status === 403
                ? "Unraid denied this request. Check the WebGUI system log for its reason."
                : "The plugin API returned HTTP " + response.status + "."));
        }
        if (result === null) throw Error("The plugin API returned an invalid response. Check the WebGUI system log.");
        return result;
    }
    function table(headers) {
        const t = document.createElement("table");
        t.className = "unraid";
        const row = t.createTHead().insertRow();
        for (const value of headers) {
            const th = document.createElement("th");
            th.textContent = value;
            row.append(th);
        }
        t.createTBody();
        return t;
    }
    function options(select, names) {
        const old = select.value;
        select.replaceChildren(...names.map(x => new Option(x, x)));
        if (names.includes(old)) select.value = old;
    }
    function updateWorkloads() {
        if (!state) return;
        const key = el("gpu").value;
        const gpu = state.devices.find(device => device.key === key);
        const detected = gpu?.containers;
        const vm = el("vm").value;
        const automatic = state.config.enabled && el("automatic").checked;
        el("trigger").textContent = vm && key ? vm + " uses " + key + " · " +
            (automatic ? "Start from VM Manager or here" : "Start from GPU Switch Manager") : "";
        const activeCount = Object.values(detected || {}).filter(reasons => reasons.includes("active client")).length;
        el("workload-summary").textContent = !key ? "Select a shared GPU to find affected containers." :
            !detected ? "Live workload detection is unavailable for this GPU." :
            activeCount ? activeCount + " container(s) have this GPU open." :
            "No running containers have this GPU open.";
        const body = el("workloads").querySelector("tbody");
        if (!body) return;
        const rows = [...body.rows];
        for (const row of rows) {
            const name = row.dataset.name;
            const reasons = detected && Object.hasOwn(detected, name) ? detected[name] : null;
            const active = Boolean(reasons?.includes("active client"));
            const policy = row.querySelector("select").value;
            row.dataset.access = active ? "2" : reasons ? "1" : "0";
            row.cells[1].textContent = active ? "Open device handle" :
                reasons ? "Possible access · " + reasons.join(", ") : "";
            row.hidden = !active && !policy && !el("show-other").checked;
        }
        rows.sort((a, b) => Number(b.dataset.access) - Number(a.dataset.access));
        body.replaceChildren(...rows);
    }
    function profile() {
        const vm = el("vm").value;
        const p = Object.hasOwn(state.config.profiles, vm) ? state.config.profiles[vm] : null;
        el("vm-message").textContent = p ? "Loaded saved profile for " + vm : (vm ? "No saved profile for " + vm : "");
        el("gpu").value = p?.gpu || "";
        el("automatic").checked = p?.automatic ?? true;
        const items = p?.containers || [];
        const names = [...new Set([...items.map(x => x.name), ...(state.containers || [])])];
        const grid = table(["Container", "GPU access detected", "Action when VM starts", "Start after (comma-separated names)"]);
        for (const name of names) {
            const row = grid.tBodies[0].insertRow();
            row.dataset.name = name;
            row.insertCell().textContent = name;
            row.insertCell();
            const policy = document.createElement("select");
            for (const [k, label] of [["", "Unassigned"], ["restore", "Stop and restore"], ["stop", "Stop only"], ["leave", "Leave running"], ["start", "Start with VM (CPU only)"]])
                policy.add(new Option(label, k));
            const item = items.find(x => x.name === name);
            policy.value = item?.policy || "";
            policy.onchange = updateWorkloads;
            row.insertCell().append(policy);
            const deps = document.createElement("input");
            deps.value = (item?.after || []).join(", ");
            row.insertCell().append(deps);
        }
        el("workloads").replaceChildren(grid);
        updateWorkloads();
        syncControls();
    }
    function renderDevices(devices) {
        const grid = table(["GPU / PCI / UUID", "Drivers / IOMMU", "Role", "Owner / VM profiles", "Workloads", "Status", ""]);
        const labels = {HOST_AVAILABLE: "Available to Unraid", HOST_IN_USE: "In use by host workloads",
            PREPARING_VM: "Switching to VM", VFIO_BOUND: "Bound to VFIO", VM_ACTIVE: "Assigned to running VM",
            RESTORING_HOST: "Returning to Unraid", ERROR: "Recovery or review required"};
        for (const d of devices) {
            const row = grid.tBodies[0].insertRow();
            for (const v of [
                [d.board_vendor, d.model, d.description, d.pci, d.uuid || d.enrolled_uuid].filter(Boolean).join("\n"),
                Object.entries(d.drivers).map(([a, driver]) => a + " " + driver + " / " + d.groups[a]).join("\n"),
                d.key ? (d.protected ? "Protected" : "Shared") : "Unmanaged",
                [d.owner || d.transaction?.vm || (d.drivers[d.pci] === "vfio-pci" ? "VFIO" : "Host"),
                    ...Object.keys(state.config.profiles).filter(vm => d.key && state.config.profiles[vm].gpu === d.key)].filter((v, i, a) => a.indexOf(v) === i).join("\n"),
                [d.containers === null ? "Container data unavailable" : Object.keys(d.containers).join(", "),
                    d.clients?.length ? d.clients.length + " GPU client(s)" : ""].filter(Boolean).join("\n"),
                [d.owner ? "Assigned to running VM" : labels[d.state], d.error || d.transaction?.message || d.last?.message].filter(Boolean).join("\n")
            ]) {
                const cell = row.insertCell();
                cell.textContent = v;
            }
            const configure = document.createElement("button");
            configure.type = "button";
            configure.textContent = "Configure";
            configure.onclick = () => {
                el("pci").value = d.pci;
                el("pci").onchange();
                el("key").focus();
            };
            const actions = row.insertCell();
            actions.append(configure);
            if (d.key && !d.protected) {
                const workloads = document.createElement("button");
                workloads.type = "button";
                workloads.textContent = "Review workloads";
                workloads.onclick = () => {
                    el("gpu").value = d.key;
                    el("gpu").onchange();
                    el("gpu").focus();
                };
                actions.append(workloads);
            }
        }
        el("devices").replaceChildren(grid);
        if (!devices.length) {
            const empty = document.createElement("p");
            empty.textContent = "No GPUs to display. Check discovery status and diagnostics.";
            el("devices").append(empty);
        }
    }
    async function refresh() {
        state = await api("status");
        renderDevices(state.devices);
        options(el("pci"), state.devices.map(d => d.pci));
        options(el("gpu"), Object.keys(state.config.gpus).filter(k => !state.config.gpus[k].protected));
        el("gpu").prepend(new Option("Select a shared GPU", ""));
        options(el("vm"), [...new Set([...Object.keys(state.config.profiles), ...state.vms])]);
        options(el("start-container"), ["", ...((state.containers || []).slice().sort())]);
        el("start-container").options[0].text = "Select a container";
        el("enabled").checked = state.config.enabled;
        el("pci").onchange();
        el("message").textContent = state.errors.join("\n") || "Discovery complete" + (state.release ? " · " + state.release.version : "");
        profile();
    }
    const show = result => { el("detail").textContent = JSON.stringify(result, null, 2); };
    function button(id, fn, message = "message") {
        el(id).onclick = async () => {
            if (busy) return;
            busy = true;
            syncControls();
            try { await fn(); } catch (e) { el(message).textContent = e.message; }
            finally { busy = false; syncControls(); }
        };
    }
    async function mutate(action, fields, prompt, message = "message") {
        if (!window.confirm(prompt)) return;
        el(message).textContent = action === "start" ? "Checking prerequisites and starting the VM…" : "Waiting for " + action;
        let active = true;
        async function progress() {
            try {
                const data = await api("progress");
                if (active) show(data);
            } catch (_) { /* The operation response reports final errors. */ }
            if (active) polling = setTimeout(progress, 1500);
        }
        progress();
        try {
            const result = await api(action, {...fields, confirmed: "yes"});
            active = false;
            await refresh();
            show(result);
            if (action === "start") {
                el(message).textContent = "VM started · " + result.vm;
                if (result.warnings?.length) el(message).textContent += " · Container startup: " + result.warnings.join("; ");
            }
        } finally { active = false; clearTimeout(polling); }
    }
    function save(config, prompt) {
        return mutate("import", {config: JSON.stringify(config), revision: state.revision}, prompt);
    }
    el("vm").onchange = profile;
    button("load-profile", refresh, "vm-message");
    el("start-container").onchange = syncControls;
    button("add-start-container", () => {
        const name = el("start-container").value;
        const row = [...el("workloads").querySelector("tbody").rows].find(row => row.dataset.name === name);
        if (!row) throw Error("Container list changed. Refresh and try again.");
        const policy = row.querySelector("select");
        if (policy.value && policy.value !== "start") throw Error("Clear the existing action for " + name + " first.");
        policy.value = "start";
        updateWorkloads();
        el("vm-message").textContent = name + " added to this profile. Select Save profile to keep the change.";
    }, "vm-message");
    el("gpu").onchange = () => { updateWorkloads(); syncControls(); };
    el("show-other").onchange = updateWorkloads;
    el("automatic").onchange = updateWorkloads;
    el("pci").onchange = () => {
        const d = state.devices.find(d => d.pci === el("pci").value);
        el("key").value = d?.key || "";
        el("role").value = d?.key && !d.protected ? "shared" : "protected";
        syncControls();
    };
    button("refresh", refresh);
    button("logs", async () => show(await api("logs")));
    button("diag", async () => show(await api("diagnostics")));
    button("enroll", () => mutate("enroll", {key: el("key").value, pci: el("pci").value, role: el("role").value}, "Save this physical GPU identity and protection setting?"));
    button("unmanage", () => {
        const key = state.devices.find(d => d.pci === el("pci").value)?.key;
        if (!key) return;
        const cfg = structuredClone(state.config);
        if (Object.values(cfg.profiles).some(p => p.gpu === key)) throw Error("Remove the GPU from its VM profiles before removing enrollment.");
        delete cfg.gpus[key];
        return save(cfg, "Remove this GPU’s enrollment and any plugin protection?");
    });
    button("enable", () => save({...state.config, enabled: el("enabled").checked}, "Change automatic VM switching?"));
    button("save", () => {
        const cfg = structuredClone(state.config);
        const containers = [];
        for (const row of el("workloads").querySelectorAll("tbody tr")) {
            const policy = row.querySelector("select").value;
            if (policy) containers.push({name: row.dataset.name, policy, after: row.querySelector("input").value.split(",").map(x => x.trim()).filter(Boolean)});
        }
        cfg.profiles[el("vm").value] = {gpu: el("gpu").value, automatic: el("automatic").checked, containers};
        return save(cfg, "Save the GPU and container policies for " + el("vm").value + "?");
    });
    button("delete", () => {
        const cfg = structuredClone(state.config);
        delete cfg.profiles[el("vm").value];
        return save(cfg, "Delete the selected VM profile?");
    });
    button("dry", async () => {
        show(await api("dry-run", {vm: el("vm").value}));
        el("vm-message").textContent = "Dry run passed. Review the planned workload stops in the details.";
    }, "vm-message");
    button("assign", () => mutate("assign", {vm: el("vm").value}, "Add the approved PCI functions to the persistent definition of " + el("vm").value + "?"));
    button("start", () => mutate("start", {vm: el("vm").value}, "Stop assigned workloads and start " + el("vm").value + "?", "vm-message"), "vm-message");
    button("shutdown", () => mutate("shutdown", {vm: el("vm").value}, "Request a graceful shutdown of " + el("vm").value + "?"));
    button("recover", () => mutate("recover", {gpu: el("gpu").value}, "Reconcile the selected GPU and restore recorded containers if safe?"));
    function download(data, filename) {
        const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: "application/json"}));
        const link = document.createElement("a");
        link.href = url; link.download = filename; link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
    button("export", async () => download(await api("export"), "gpu-sharing.json"));
    button("download", async () => download({report: await api("diagnostics"), operations: await api("logs")}, "gpu-switch-manager-diagnostics.json"));
    button("import", async () => {
        const file = el("file").files[0];
        if (!file || file.size > 262144) throw Error("Select a configuration file up to 256 KiB");
        await save(JSON.parse(await file.text()), "Replace GPU identities, protection settings and VM profiles with this file?");
    });
    syncControls();
    el("refresh").click();
    async function liveRefresh() {
        if (!el("devices")) return;
        if (state && !busy && !document.hidden && el("live").checked) {
            const previous = state;
            try {
                const next = await api("status");
                if (!busy && previous === state && next.revision === state.revision) {
                    state.devices = next.devices;
                    renderDevices(next.devices);
                    updateWorkloads();
                    if (next.errors.length) el("message").textContent = next.errors.join("\n");
                }
            }
            catch (e) { if (!busy && previous === state) el("message").textContent = e.message; }
        }
        setTimeout(liveRefresh, 15000);
    }
    setTimeout(liveRefresh, 15000);
})();
