// Thin client for the backend REST API.
const JSON_HEADERS = { "Content-Type": "application/json" };

export async function fetchTodos() {
  const res = await fetch("/api/todos");
  if (!res.ok) throw new Error(`list failed: ${res.status}`);
  return res.json();
}

export async function fetchTodo(id) {
  const res = await fetch(`/api/todos/${id}`);
  return res.json();
}

export async function createTodo(title) {
  const res = await fetch("/api/todos", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ title }),
  });
  return res.json();
}
