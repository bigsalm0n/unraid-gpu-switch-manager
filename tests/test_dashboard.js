"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {test} = require("node:test");

class Element {
    constructor(tag) {
        this.tag = tag;
        this.children = [];
        this.dataset = {};
        this.value = "";
    }

    add(element) { this.append(element); }
    append(...elements) { this.children.push(...elements); }
    prepend(...elements) { this.children.unshift(...elements); }
    replaceChildren(...elements) { this.children = elements; }
    click() { return this.disabled ? undefined : this.onclick(); }
    focus() {}
    createTHead() { return new Element(); }
    createTBody() { this.tBodies = [new Element()]; }
    insertRow() { const row = new Element(); this.append(row); return row; }
    insertCell() { const cell = new Element(); this.append(cell); return cell; }
    get options() { return this.children; }
    get cells() { return this.children; }
    get rows() { return this.children; }
    querySelector(tag) {
        if (tag === "tbody") return this.children[0]?.tBodies?.[0];
        return this.children.flatMap(child => child.children || []).find(child => child.tag === tag);
    }
    querySelectorAll() { return []; }
}

function fixture(responses, token = 'token with + & =') {
    const html = fs.readFileSync("ui/dashboard.inc", "utf8");
    const elements = Object.fromEntries([...html.matchAll(/id="(gsm(?:-[^"]+)?)"/g)]
        .map(([, id]) => [id, new Element()]));
    const root = elements.gsm;
    root.dataset.csrf = token;
    root.querySelectorAll = () => Object.entries(elements)
        .filter(([key]) => key !== "gsm").map(([key, element]) => Object.assign(element, {id: key}));
    const calls = [];
    vm.runInNewContext(fs.readFileSync("ui/dashboard.js", "utf8"), {
        document: {getElementById: id => elements[id], createElement: tag => new Element(tag)},
        Option: class { constructor(text, value) { this.text = text; this.value = value; } },
        URLSearchParams,
        window: {confirm: () => true},
        setTimeout() {},
        clearTimeout() {},
        fetch(url, request) {
            calls.push({url, request});
            return Promise.resolve(responses.shift());
        }
    });
    return {elements, calls};
}

function response(status, body) {
    return {status, ok: status === 200, type: "basic", json: () => body === null
        ? Promise.reject(Error("HTML or empty response")) : Promise.resolve(body)};
}

const tick = () => new Promise(resolve => setImmediate(resolve));

test("waits for discovery, posts native form fields, and initializes GPU and VM selectors", async () => {
    const payload = {
        devices: [{
            pci: "0000:62:00.0", model: "NVIDIA test card", key: "game", drivers: {"0000:62:00.0": "nvidia"},
            groups: {"0000:62:00.0": "45"}, containers: {}, clients: [], state: "HOST_AVAILABLE"
        }],
        config: {gpus: {game: {protected: false}}, profiles: {}, enabled: false},
        vms: ["Windows"], containers: [], errors: []
    };
    const {elements, calls} = fixture([response(200, payload)]);
    assert.equal(elements["gsm-save"].disabled, true);
    await tick();
    assert.equal(calls[0].url, "/plugins/gpu-switch-manager/api.php");
    assert.equal(calls[0].request.credentials, "same-origin");
    assert.equal(calls[0].request.method, "POST");
    assert.equal(calls[0].request.body.get("action"), "status");
    assert.equal(calls[0].request.body.get("csrf_token"), "token with + & =");
    assert.match(new Request("http://example.test", calls[0].request).headers.get("content-type"),
        /^application\/x-www-form-urlencoded/);
    assert.equal(elements["gsm-pci"].children[0].value, "0000:62:00.0");
    assert.equal(elements["gsm-vm"].children[0].value, "Windows");
    assert.equal(elements["gsm-gpu"].children[1].value, "game");
    assert.equal(elements["gsm-message"].textContent, "Discovery complete");
});

test("authentication, CSRF, generic HTTP, backend and missing page token errors stay distinct", async () => {
    for (const [input, message] of [
        [{type: "opaqueredirect", status: 0}, /requires sign-in/],
        [response(403, {error: "wrong csrf_token"}), /rejected the request token/],
        [response(403, null), /denied this request/],
        [response(502, {error: "Backend execution failed"}), /Backend execution failed/],
        [response(200, null), /invalid response/]
    ]) {
        const {elements} = fixture([input]);
        await tick();
        assert.match(elements["gsm-message"].textContent, message);
        assert.equal(elements["gsm-save"].disabled, true);
        assert.equal(elements["gsm-refresh"].disabled, false);
    }
    const {elements, calls} = fixture([], "");
    await tick();
    assert.match(elements["gsm-message"].textContent, /did not provide a request token/);
    assert.equal(calls.length, 0);
});

test("loads a saved VM profile and displays a refused start beside the controls", async () => {
    const payload = {
        devices: [], vms: ["Another VM", "Gaming"], containers: ["voice"], errors: [],
        config: {gpus: {game: {protected: false}}, enabled: true,
            profiles: {Gaming: {gpu: "game", automatic: false, containers: [{name: "voice", policy: "restore", after: []}]}}}
    };
    const refusal = "PCI function 0000:05:00.1 is already bound to VFIO.";
    const {elements, calls} = fixture([response(200, payload), response(200, payload), response(200, {}), response(409, {error: refusal})]);
    await tick();
    elements["gsm-vm"].value = "Gaming";
    elements["gsm-vm"].onchange();
    elements["gsm-gpu"].value = "";
    await elements["gsm-load-profile"].click();
    assert.equal(elements["gsm-vm"].value, "Gaming");
    assert.equal(elements["gsm-gpu"].value, "game");
    assert.equal(elements["gsm-automatic"].checked, false);
    assert.equal(elements["gsm-workloads"].children[0].tBodies[0].children[0].children[2].children[0].value, "restore");
    await elements["gsm-start"].click();
    assert.equal(calls[1].request.body.get("action"), "status");
    assert.equal(calls[3].request.body.get("vm"), "Gaming");
    assert.equal(elements["gsm-vm-message"].textContent, refusal);
});

test("shows detected workloads for the selected GPU and keeps other containers available", async () => {
    const gpu = (key, pci, containers) => ({key, pci, model: key, drivers: {[pci]: "nvidia"},
        groups: {[pci]: "12"}, containers, clients: [], state: "HOST_AVAILABLE"});
    const payload = {
        devices: [gpu("game", "0000:05:00.0", {voice: ["active client"]}),
            gpu("other", "0000:06:00.0", {media: ["visible all"]})],
        vms: ["Gaming"], containers: ["media", "voice", "helper"], errors: [],
        config: {gpus: {game: {protected: false}, other: {protected: false}}, enabled: true, profiles: {}}
    };
    const {elements} = fixture([response(200, payload)]);
    await tick();
    elements["gsm-vm"].value = "Gaming";
    elements["gsm-gpu"].value = "game";
    elements["gsm-gpu"].onchange();
    const rows = () => elements["gsm-workloads"].querySelector("tbody").rows;
    assert.equal(rows()[0].dataset.name, "voice");
    assert.match(rows()[0].cells[1].textContent, /Open device handle/);
    assert.match(elements["gsm-trigger"].textContent, /Gaming uses game/);
    assert.equal(rows().find(row => row.dataset.name === "helper").hidden, true);
    elements["gsm-show-other"].checked = true;
    elements["gsm-show-other"].onchange();
    assert.equal(rows().find(row => row.dataset.name === "helper").hidden, false);
    elements["gsm-start-container"].value = "helper";
    elements["gsm-start-container"].onchange();
    await elements["gsm-add-start-container"].click();
    assert.equal(rows().find(row => row.dataset.name === "helper").querySelector("select").value, "start");
    elements["gsm-gpu"].value = "other";
    elements["gsm-gpu"].onchange();
    assert.equal(rows()[0].dataset.name, "media");
    assert.match(rows()[0].cells[1].textContent, /Possible access · visible all/);
});
