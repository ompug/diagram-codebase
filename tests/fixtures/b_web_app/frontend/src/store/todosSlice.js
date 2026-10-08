import { configureStore, createSlice } from "@reduxjs/toolkit";

const todosSlice = createSlice({
  name: "todos",
  initialState: { items: [], loading: false },
  reducers: {
    setTodos(state, action) {
      state.items = action.payload;
    },
    addTodo(state, action) {
      state.items.push(action.payload);
    },
  },
});

export const { setTodos, addTodo } = todosSlice.actions;
export const store = configureStore({ reducer: { todos: todosSlice.reducer } });
