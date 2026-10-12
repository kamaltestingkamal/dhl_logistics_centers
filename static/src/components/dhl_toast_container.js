/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const ICONS = { success: "✓", error: "✕", warning: "!", info: "i" };

export class DhlToastContainer extends Component {
    static template = "dhl_logistics_center.ToastContainer";
    static props = {};

    setup() {
        this.dhl = useService("dhl_notification");
        this.action = useService("action");
        this.state = useState(this.dhl.state);
    }

    icon(type) {
        return ICONS[type] || ICONS.info;
    }

    close(toast, ev) {
        ev.stopPropagation();
        this.dhl.dismissToast(toast.uid);
    }

    async open(toast) {
        this.dhl.dismissToast(toast.uid);
        if (toast.res_model && toast.res_id) {
            this.dhl.markRead([toast.id]);
            await this.action.doAction({
                type: "ir.actions.act_window",
                res_model: toast.res_model,
                res_id: toast.res_id,
                views: [[false, "form"]],
                target: "current",
            });
        }
    }
}

registry.category("main_components").add("DhlToastContainer", { Component: DhlToastContainer });
