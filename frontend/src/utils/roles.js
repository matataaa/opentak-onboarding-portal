// Roles only an administrator may grant or remove (same rule as is_privileged_role in app/rbac.py)
const PRIVILEGED_ROLE_NAMES = ['administrator', 'admin'];

export const isPrivilegedRole = (name) => PRIVILEGED_ROLE_NAMES.includes((name || '').trim().toLowerCase());
