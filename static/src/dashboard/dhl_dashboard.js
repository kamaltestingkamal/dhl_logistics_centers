/** @odoo-module **/
import { Component, onMounted, onWillStart, onWillUnmount, useEffect, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { deserializeDateTime } from "@web/core/l10n/dates";

const TONE = {
    // orders
    draft: "grey", pending: "amber", processing: "blue", shipped: "blue", delivered: "green",
    cancelled: "grey", failed: "red",
    // shipments
    ready: "blue", validating: "amber", created: "blue", in_transit: "blue",
    creation_failed: "red", delivery_failed: "red",
    // tracking
    out_for_delivery: "blue", pre_transit: "grey", returned: "red", exception: "red", unknown: "grey",
};
const LABELS = { creation_failed: "Creation failed", delivery_failed: "Delivery failed", in_transit: "In transit" };
const REFRESH_MS = 60000;

export class DhlDashboard extends Component {
    static template = "dhl_logistics_center.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notifs = useService("dhl_notification");
        this.shared = useState(this.notifs.state);
        this.state = useState({ loading: true, error: null, data: null, query: "", checking: false, updatedAt: null, tick: 0 });
        onWillStart(() => this.load());
        onMounted(() => {
            this.timer = setInterval(() => {
                this.state.tick++;
                this.load(true);
            }, REFRESH_MS);
        });
        onWillUnmount(() => clearInterval(this.timer));
        // Refresh quietly when a notification arrives or the DHL status changes.
        useEffect(
            () => {
                if (this.state.data) {
                    this.load(true);
                }
            },
            () => [this.shared.unread, this.shared.connection.dhl.state]
        );
    }

    async load(silent = false) {
        if (!silent) {
            this.state.loading = true;
        }
        try {
            this.state.data = await this.orm.call("dhl.dashboard", "get_data", []);
            this.state.error = null;
            this.state.updatedAt = new Date();
        } catch (e) {
            if (!silent || !this.state.data) {
                this.state.error = (e && e.data && e.data.message) || "The dashboard could not be loaded.";
            }
        }
        this.state.loading = false;
    }

    // ------------------------------------------------------------ view helpers
    tone(state) { return TONE[state] || "grey"; }
    label(state) {
        if (!state) { return ""; }
        return LABELS[state] || state.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
    }
    ago(str) {
        try { return deserializeDateTime(str).toRelative(); } catch { return ""; }
    }
    money(value) {
        const { symbol, position } = this.state.data.currency;
        const n = (value || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        return position === "before" ? `${symbol}${n}` : `${n} ${symbol}`;
    }
    get todayLabel() {
        return new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
    }
    get updatedLabel() {
        this.state.tick; // re-evaluated on the minute tick
        if (!this.state.updatedAt) { return ""; }
        const mins = Math.floor((Date.now() - this.state.updatedAt.getTime()) / 60000);
        return mins < 1 ? "just now" : `${mins} min ago`;
    }

    get dhl() { return this.state.data.connection.dhl; }
    get amazon() { return this.state.data.connection.amazon; }
    get agent() { return this.state.data.agent; }

    get dhlTone() {
        return { connected: "green", disconnected: "red", auth_error: "yellow", api_error: "orange" }[this.dhl.state] || "red";
    }
    get amazonTone() {
        return { connected: "green", disconnected: "red", auth_error: "yellow", api_error: "orange" }[this.amazon.state] || "red";
    }

    get attentionTone() {
        const items = this.state.data.attention;
        if (!items.length) { return "green"; }
        return items.some((i) => i.severity === "error") ? "red" : "amber";
    }

    get stations() {
        const o = this.state.data.orders_by_state;
        const s = this.state.data.shipments_by_state;
        const ships = (name, states, hint, extra = {}) => ({
            key: name, label: name, hint, model: "dhl.shipment", domain: [["state", "in", states]],
            value: states.reduce((n, k) => n + (s[k] || 0), 0), ...extra,
        });
        return [
            {
                key: "orders", label: "Orders to process", hint: "Pending or processing", model: "dhl.order",
                domain: [["state", "in", ["pending", "processing"]]], value: o.pending + o.processing
            },
            ships("Preparing", ["draft", "ready", "validating"], "Draft and ready shipments"),
            ships("At DHL", ["created"], "Created, label stored"),
            ships("In transit", ["in_transit"], "On the way"),
            ships("Delivered", ["delivered"], "Handed over"),
            ships("Needs action", ["creation_failed", "delivery_failed"], "Failed shipments", { danger: true }),
        ];
    }

    get ledger() {
        const d = this.state.data;
        return [
            { label: "Orders", value: d.total_orders, sub: d.today.orders ? `+${d.today.orders} today` : "", model: "dhl.order", domain: [] },
            { label: "Shipments", value: d.total_shipments, sub: d.today.shipments ? `+${d.today.shipments} today` : "", model: "dhl.shipment", domain: [] },
            { label: "Open returns", value: d.returns, sub: "", model: "dhl.return", domain: [["state", "not in", ["completed", "cancelled"]]] },
            { label: "Shipping cost", value: this.money(d.shipping_cost), sub: "", model: "dhl.shipment", domain: [["state", "!=", "cancelled"]] },
        ];
    }

    get orderRows() {
        const d = this.state.data;
        const max = Math.max(1, ...Object.values(d.orders_by_state));
        return Object.entries(d.orders_by_state).map(([key, count]) => ({ key, count, pct: (count / max) * 100 }));
    }

    get chart() {
        const days = this.state.data.shipments_per_day;
        const max = Math.max(1, ...days.map((x) => x.count));
        return {
            total: days.reduce((n, x) => n + x.count, 0),
            max,
            bars: days.map((x, i) => ({
                ...x, h: x.count ? Math.max(6, (x.count / max) * 100) : 0,
                today: i === days.length - 1,
                showLabel: i === 0 || i === days.length - 1 || i === Math.floor(days.length / 2),
            })),
        };
    }

    // ------------------------------------------------------------ navigation
    openList(name, model, domain) {
        return this.action.doAction({
            type: "ir.actions.act_window", name, res_model: model, domain,
            views: [[false, "list"], [false, "form"]], target: "current",
        });
    }
    openStation(st) { return this.openList(st.label, st.model, st.domain); }
    openLedger(row) { return this.openList(row.label, row.model, row.domain); }
    openRecord(model, id) {
        return this.action.doAction({
            type: "ir.actions.act_window", res_model: model, res_id: id, views: [[false, "form"]], target: "current",
        });
    }
    openTarget(item) {
        const t = item.target;
        if (!t) { return; }
        if (t.model === "dhl.connection") { return this.openConnection(); }
        return this.openList(t.name, t.model, t.domain);
    }
    openConnection() {
        return this.dhl.id ? this.openRecord("dhl.connection", this.dhl.id)
            : this.action.doAction("dhl_logistics_center.action_dhl_connection_new");
    }
    openAgent() { return this.action.doAction("dhl_logistics_center.action_dhl_agent"); }
    openAmazon() { return this.action.doAction("dhl_logistics_center.action_dhl_amazon_connection"); }
    newOrder() {
        return this.action.doAction({ type: "ir.actions.act_window", res_model: "dhl.order", views: [[false, "form"]], target: "current" });
    }
    async runCheck() {
        if (!this.agent.id || this.state.checking) { return; }
        this.state.checking = true;
        try {
            await this.orm.call("dhl.agent", "action_run_check", [[this.agent.id]]);
        } finally {
            this.state.checking = false;
        }
        await this.load(true);
    }
    onSearchKey(ev) {
        if (ev.key !== "Enter" || !this.state.query.trim()) { return; }
        const q = this.state.query.trim();
        this.openList(`Search: ${q}`, "dhl.shipment", [
            "|", "|", "|",
            ["name", "ilike", q], ["tracking_number", "ilike", q],
            ["consignee_name", "ilike", q], ["order_id", "ilike", q],
        ]);
    }
}

registry.category("actions").add("dhl_logistics_center.dashboard", DhlDashboard);
