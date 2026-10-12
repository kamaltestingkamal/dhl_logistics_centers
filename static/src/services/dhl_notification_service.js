/** @odoo-module **/
import { registry } from "@web/core/registry";
import { reactive } from "@odoo/owl";

const TOAST_MS = { success: 5000, info: 6000, warning: 8000, error: 12000 };
const MAX_TOASTS = 4;

/**
 * Shared state for DHL toasts, the notification bell and the navbar connection
 * badge. dhl.notification.create() pushes every new notification over the bus
 * ("dhl.notification"); we show a toast and refresh the persistent list.
 */
export const dhlNotificationService = {
    dependencies: ["bus_service", "orm"],
    start(env, { bus_service, orm }) {
        const state = reactive({
            toasts: [],
            items: [],
            unread: 0,
            connection: { dhl: { connected: false, has_connection: false, state: "disconnected", label: "DHL Disconnected" } },
        });
        const timers = new Map(); // uid -> { id, deadline }
        let seq = 0;

        function dismissToast(uid) {
            const t = timers.get(uid);
            if (t) {
                clearTimeout(t.id);
                timers.delete(uid);
            }
            const idx = state.toasts.findIndex((x) => x.uid === uid);
            if (idx >= 0) {
                state.toasts.splice(idx, 1);
            }
        }

        function schedule(uid, ms) {
            timers.set(uid, { id: setTimeout(() => dismissToast(uid), ms), deadline: Date.now() + ms });
        }

        // Hovering a toast pauses its countdown (the CSS progress bar pauses too).
        function pause(uid) {
            const t = timers.get(uid);
            if (t) {
                clearTimeout(t.id);
                t.remaining = Math.max(500, t.deadline - Date.now());
            }
        }
        function resume(uid) {
            const t = timers.get(uid);
            if (t && t.remaining) {
                schedule(uid, t.remaining);
            }
        }

        function toast(payload) {
            const uid = ++seq;
            const duration = TOAST_MS[payload.type] || 6000;
            state.toasts.push({ ...payload, uid, duration });
            while (state.toasts.length > MAX_TOASTS) {
                dismissToast(state.toasts[0].uid);
            }
            schedule(uid, duration);
        }

        async function refresh() {
            try {
                const data = await orm.call("dhl.notification", "get_panel_data", []);
                state.items = data.items;
                state.unread = data.unread;
            } catch {
                /* user has no DHL access: nothing to show */
            }
        }

        async function refreshConnection() {
            try {
                state.connection = await orm.call("dhl.dashboard", "get_connection_status", []);
            } catch {
                /* ignore */
            }
        }

        async function markRead(ids) {
            await orm.call("dhl.notification", "mark_read", [ids]);
            await refresh();
        }

        async function markAllRead() {
            await orm.call("dhl.notification", "mark_all_read", []);
            await refresh();
        }

        bus_service.subscribe("dhl.notification", (payload) => {
            toast(payload);
            refresh();
            refreshConnection();
        });
        bus_service.start();

        return { state, toast, dismissToast, pause, resume, refresh, refreshConnection, markRead, markAllRead };
    },
};

registry.category("services").add("dhl_notification", dhlNotificationService);
