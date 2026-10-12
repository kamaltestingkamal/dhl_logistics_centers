/** @odoo-module **/
import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";

/** Honest placeholder for planned features. Content comes from the client action's params. */
export class DhlComingSoon extends Component {
    static template = "dhl_logistics_center.ComingSoon";
    static props = ["*"];

    get info() {
        return (this.props.action && this.props.action.params) || {};
    }
}

registry.category("actions").add("dhl_logistics_center.coming_soon", DhlComingSoon);
