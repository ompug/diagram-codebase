"""Data access helpers for todos."""

from sqlalchemy.orm import Session

from . import models


def get_todos(db: Session, limit: int = 100):
    return db.query(models.Todo).limit(limit).all()


def get_todo(db: Session, todo_id: int):
    return db.query(models.Todo).filter(models.Todo.id == todo_id).first()


def create_todo(db: Session, title: str):
    todo = models.Todo(title=title)
    db.add(todo)
    db.commit()
    db.refresh(todo)
    return todo
