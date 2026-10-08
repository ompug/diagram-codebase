"""FastAPI application exposing the todo REST API."""

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

from . import crud
from .database import get_db
from .schemas import TodoCreate, TodoOut

app = FastAPI(title="Todos")


@app.get("/api/todos", response_model=list[TodoOut])
def list_todos(db: Session = Depends(get_db)):
    return crud.get_todos(db)


@app.post("/api/todos", response_model=TodoOut, status_code=201)
def add_todo(payload: TodoCreate, db: Session = Depends(get_db)):
    return crud.create_todo(db, payload.title)


@app.get("/api/todos/{todo_id}", response_model=TodoOut)
def read_todo(todo_id: int, db: Session = Depends(get_db)):
    todo = crud.get_todo(db, todo_id)
    if todo is None:
        raise HTTPException(status_code=404, detail="todo not found")
    return todo
