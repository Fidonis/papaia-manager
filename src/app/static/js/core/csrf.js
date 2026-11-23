// The CSRF token of this session, read from the frame's own forms (every one of them carries
// it as `pk_csrf`), so the page needs no second copy of it in the markup.

export function csrfToken(doc = document) {
  const field = doc.querySelector("input[name=pk_csrf]");
  return field ? field.value : "";
}
