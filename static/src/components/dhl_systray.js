/** @odoo-module **/
import { Component, onWillStart, useExternalListener, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { user } from "@web/core/user";
import { deserializeDateTime } from "@web/core/l10n/dates";

const ICONS = { success: "✓", error: "✕", warning: "!", info: "i" };
const ROLE_GROUPS = [
    "dhl_logistics_center.group_dhl_user",
    "dhl_logistics_center.group_dhl_operator",
    "dhl_logistics_center.group_dhl_readonly",
];
const QUICK_CREATE = [
    { label: "New order", icon: "fa-shopping-basket", model: "dhl.order" },
    { label: "New product", icon: "fa-cube", model: "dhl.product" },
    { label: "New customer", icon: "fa-user", model: "dhl.customer" },
    { label: "New shipment", icon: "fa-truck", model: "dhl.shipment" },
    { label: "New return", icon: "fa-undo", model: "dhl.return" },
    { label: "Connect DHL", icon: "fa-plug", connect: true },
];

/** Navbar: DHL connection badge, "+" quick create, and the notification bell. */
export class DhlSystray extends Component {
    static template = "dhl_logistics_center.Systray";
    static props = {};

    setup() {
        this.dhl = useService("dhl_notification");
        this.action = useService("action");
        this.state = useState({ visible: false, open: null, tab: "all" });
        this.shared = useState(this.dhl.state);
        this.root = useRef("root");
        this.quickCreate = QUICK_CREATE;
        useExternalListener(window, "click", (ev) => {
            if (this.root.el && !this.root.el.contains(ev.target)) {
                this.state.open = null;
            }
        });
        useExternalListener(window, "keydown", (ev) => {
            if (ev.key === "Escape") {
                this.state.open = null;
            }
        });
        onWillStart(async () => {
            const checks = await Promise.all(ROLE_GROUPS.map((g) => user.hasGroup(g)));
            this.state.visible = checks.some(Boolean);
            if (this.state.visible) {
                await Promise.all([this.dhl.refresh(), this.dhl.refreshConnection()]);
            }
        });
    }

    get dhlStatus() {
        return this.shared.connection.dhl;
    }

    get shownItems() {
        const items = this.shared.items;
        return this.state.tab === "unread" ? items.filter((i) => !i.is_read) : items;
    }

    toggle(name) {
        this.state.open = this.state.open === name ? null : name;
    }

    icon(type) {
        return ICONS[type] || ICONS.info;
    }

    timeAgo(item) {
        try {
            return deserializeDateTime(item.create_date).toRelative();
        } catch {
            return "";
        }
    }

    async openAmazon() {
        this.state.open = null;
        await this.action.doAction("dhl_logistics_center.action_dhl_amazon_connection");
    }

    async openConnection() {
        this.state.open = null;
        const dhl = this.dhlStatus;
        if (dhl.id) {
            await this.action.doAction({
                type: "ir.actions.act_window",
                res_model: "dhl.connection",
                res_id: dhl.id,
                views: [[false, "form"]],
                target: "current",
            });
        } else {
            await this.action.doAction("dhl_logistics_center.action_dhl_connection_new");
        }
    }

    async quick(entry) {
        this.state.open = null;
        if (entry.connect) {
            return this.openConnection();
        }
        await this.action.doAction({
            type: "ir.actions.act_window",
            res_model: entry.model,
            views: [[false, "form"]],
            target: "current",
        });
    }

    async openItem(item) {
        this.state.open = null;
        if (!item.is_read) {
            await this.dhl.markRead([item.id]);
        }
        if (item.res_model && item.res_id) {
            await this.action.doAction({
                type: "ir.actions.act_window",
                res_model: item.res_model,
                res_id: item.res_id,
                views: [[false, "form"]],
                target: "current",
            });
        }
    }

    async markAllRead() {
        await this.dhl.markAllRead();
    }

    async openAll() {
        this.state.open = null;
        await this.action.doAction("dhl_logistics_center.action_dhl_notification");
    }
}

registry.category("systray").add("dhl_logistics_center.systray", { Component: DhlSystray }, { sequence: 3 });
