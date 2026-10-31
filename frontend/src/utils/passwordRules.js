// Mirrors new_password_error in app/ots.py: printable ASCII only, none of & ^ $ < > : @,
// no space at the start or end. Length is checked separately by each form.
const ALLOWED_CHARS = /^(?:(?![&^$<>:@])[\x20-\x7E])+$/;

export function isValidNewPassword(password) {
  return typeof password === 'string'
    && ALLOWED_CHARS.test(password)
    && !password.startsWith(' ')
    && !password.endsWith(' ');
}
