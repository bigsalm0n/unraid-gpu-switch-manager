(() => {
    "use strict";
    const el = id => document.getElementById("gsm-" + id);
    let state, polling;
    async function api(action, fields = {}) {
        const token = typeof csrf_token === "string" ? csrf_token : document.querySelector('input[name=csrf_token]')?.value;
        const response = await fetch("/plugins/gpu-switch-manager/api.php", {
            method: "POST", credentials: "same-origin",
            body: new URLSearchParams({action, csrf_token: token || "", ...fields})
        });
        const result = await response.json();
        if (!response.ok) throw Error(result.error || response.statusText);
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
    function profile() {
        const p = state.config.profiles[el("vm").value];
        if (p) el("gpu").value = p.gpu;
        const items = p?.containers || [];
        const names = [...new Set([...items.map(x => x.name), ...(state.containers || [])])];
        const grid = table(["Container", "Switch policy", "Start after (comma-separated names)"]);
        for (const name of names) {
            const row = grid.tBodies[0].insertRow();
            row.dataset.name = name;
            row.insertCell().textContent = name;
            const policy = document.createElement("select");
            for (const [k, label] of [["", "Unassigned"], ["restore", "Stop and restore"], ["stop", "Stop only"], ["leave", "Leave running"]])
                policy.add(new Option(label, k));
            const item = items.find(x => x.name === name);
            policy.value = item?.policy || "";
            row.insertCell().append(policy);
            const deps = document.createElement("input");
            deps.value = (item?.after || []).join(", ");
            row.insertCell().append(deps);
        }
        el("workloads").replaceChildren(grid);
    }
    function renderDevices(devices) {
        const grid = table(["GPU / PCI / UUID", "Drivers / IOMMU", "Telemetry", "Ownership", "Containers", "Last operation"]);
        for (const d of devices) {
            const row = grid.tBodies[0].insertRow();
            for (const v of [
                [d.board_vendor, d.model, d.description, d.pci, d.uuid || d.enrolled_uuid].filter(Boolean).join("\n"),
                Object.entries(d.drivers).map(([a, driver]) => a + " " + driver + " / " + d.groups[a]).join("\n"),
                [d.temperature, d.utilization, d.memory_used && d.memory_used + " / " + d.memory_total].filter(Boolean).join("\n"),
                [d.state, d.owner || d.transaction?.vm || (d.drivers[d.pci] === "vfio-pci" ? "VFIO" : "Host"), d.protected ? "Protected" : ""].filter(Boolean).join("\n"),
                d.containers === null ? "Unavailable" : Object.keys(d.containers).join(", "),
                d.error || d.transaction?.message || d.last?.message || ""
            ]) {
                const cell = row.insertCell();
                cell.style.whiteSpace = "pre-line";
                cell.textContent = v;
            }
        }
        el("devices").replaceChildren(grid);
    }
    async function refresh() {
        state = await api("status");
        renderDevices(state.devices);
        options(el("pci"), state.devices.map(d => d.pci));
        options(el("gpu"), Object.keys(state.config.gpus).filter(k => !state.config.gpus[k].protected));
        options(el("vm"), [...new Set([...state.vms, ...Object.keys(state.config.profiles)])]);
        el("enabled").checked = state.config.enabled;
        el("pci").onchange();
        el("message").textContent = state.errors.join("\n") || "Discovery complete" + (state.release ? " · " + state.release.version : "");
        profile();
    }
    const show = result => { el("detail").textContent = JSON.stringify(result, null, 2); };
    function button(id, fn) {
        el(id).onclick = async () => {
            el(id).disabled = true;
            try { await fn(); } catch (e) { el("message").textContent = e.message; }
            finally { el(id).disabled = false; }
        };
    }
    async function mutate(action, fields, prompt) {
        if (!window.confirm(prompt)) return;
        el("message").textContent = "Waiting for " + action;
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
        } finally { active = false; clearTimeout(polling); }
    }
    function save(config, prompt) {
        return mutate("import", {config: JSON.stringify(config), revision: state.revision}, prompt);
    }
    el("vm").onchange = profile;
    el("pci").onchange = () => {
        const d = state.devices.find(d => d.pci === el("pci").value);
        el("key").value = d?.key || "";
        el("role").value = d?.key && !d.protected ? "shared" : "protected";
    };
    button("refresh", refresh);
    button("logs", async () => show(await api("logs")));
    button("diag", async () => show(await api("diagnostics")));
    button("enroll", () => mutate("enroll", {key: el("key").value, pci: el("pci").value, role: el("role").value}, "Save this physical GPU identity and protection setting?"));
    button("enable", () => save({...state.config, enabled: el("enabled").checked}, "Change automatic VM switching?"));
    button("save", () => {
        const cfg = structuredClone(state.config);
        const containers = [];
        for (const row of el("workloads").querySelectorAll("tbody tr")) {
            const policy = row.querySelector("select").value;
            if (policy) containers.push({name: row.dataset.name, policy, after: row.querySelector("input").value.split(",").map(x => x.trim()).filter(Boolean)});
        }
        cfg.profiles[el("vm").value] = {gpu: el("gpu").value, containers};
        return save(cfg, "Save the GPU and container policies for " + el("vm").value + "?");
    });
    button("delete", () => {
        const cfg = structuredClone(state.config);
        delete cfg.profiles[el("vm").value];
        return save(cfg, "Delete the selected VM profile?");
    });
    button("dry", async () => show(await api("dry-run", {vm: el("vm").value})));
    button("assign", () => mutate("assign", {vm: el("vm").value}, "Add the approved PCI functions to the persistent definition of " + el("vm").value + "?"));
    button("start", () => mutate("start", {vm: el("vm").value}, "Stop assigned workloads and start " + el("vm").value + "?"));
    button("shutdown", () => mutate("shutdown", {vm: el("vm").value}, "Request a graceful shutdown of " + el("vm").value + "?"));
    button("recover", () => mutate("recover", {gpu: el("gpu").value}, "Reconcile the selected GPU and restore recorded containers if safe?"));
    button("export", async () => {
        const url = URL.createObjectURL(new Blob([JSON.stringify(await api("export"), null, 2)], {type: "application/json"}));
        const link = document.createElement("a");
        link.href = url; link.download = "gpu-sharing.json"; link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    button("import", async () => {
        const file = el("file").files[0];
        if (!file || file.size > 262144) throw Error("Select a configuration file up to 256 KiB");
        await save(JSON.parse(await file.text()), "Replace GPU identities, protection settings and VM profiles with this file?");
    });
    refresh().catch(e => { el("message").textContent = e.message; });
    async function liveRefresh() {
        if (!el("devices")) return;
        if (!document.hidden && el("live").checked) {
            try { renderDevices((await api("status")).devices); }
            catch (e) { el("message").textContent = e.message; }
        }
        setTimeout(liveRefresh, 15000);
    }
    setTimeout(liveRefresh, 15000);
})();
