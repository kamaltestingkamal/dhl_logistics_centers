from . import models


def post_init_hook(env):
    """Make the installing admin a DHL manager and operator.

    Done in Python (not XML) so it does not depend on the exact name of the
    users<->groups relational field, which differs between Odoo versions.
    """
    manager_group = env.ref('dhl_logistics_center.group_dhl_manager')
    operator_group = env.ref('dhl_logistics_center.group_dhl_operator')
    fname = 'user_ids' if 'user_ids' in manager_group._fields else 'users'
    users = env['res.users'].browse([env.ref('base.user_root').id, env.ref('base.user_admin').id])
    manager_group.write({fname: [(4, u.id) for u in users.exists()]})
    operator_group.write({fname: [(4, u.id) for u in users.exists()]})
