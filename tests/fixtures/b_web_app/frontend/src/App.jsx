import React, { useEffect, useState } from "react";
import { useDispatch, useSelector } from "react-redux";
import { createTodo, fetchTodos } from "./api";
import { addTodo, setTodos } from "./store/todosSlice";

export function TodoItem({ todo }) {
  return <li className={todo.done ? "done" : ""}>{todo.title}</li>;
}

export default function App() {
  const dispatch = useDispatch();
  const todos = useSelector((s) => s.todos.items);
  const [title, setTitle] = useState("");

  useEffect(() => {
    fetchTodos().then((items) => dispatch(setTodos(items)));
  }, [dispatch]);

  const onSubmit = async (event) => {
    event.preventDefault();
    const todo = await createTodo(title);
    dispatch(addTodo(todo));
    setTitle("");
  };

  return (
    <form onSubmit={onSubmit}>
      <input value={title} onChange={(e) => setTitle(e.target.value)} />
      <ul>
        {todos.map((t) => (
          <TodoItem key={t.id} todo={t} />
        ))}
      </ul>
    </form>
  );
}
