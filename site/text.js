/** Match Python str slicing: catalog text limits count Unicode code points,
 * not JavaScript UTF-16 code units. Byte limits remain separate. */
export function isText(value, maxLength, allowEmpty = false) {
  if (typeof value !== 'string' || (!allowEmpty && value.length === 0)) return false;
  if (!Number.isSafeInteger(maxLength) || maxLength < 0) return false;
  // A Unicode code point occupies at most two UTF-16 code units.
  if (value.length > maxLength * 2) return false;
  let length = 0;
  for (const _point of value) {
    if (++length > maxLength) return false;
  }
  return true;
}
