"""Routes with unusual paths 🚀 and names."""

from flask import Flask

app = Flask(__name__)
API_KEY = "sk_test_FAKE_DO_NOT_USE_0000000000"


@app.route("/v1/naïve-café/<int:item_id>", methods=["GET", "DELETE"])
def café_item(item_id: int):
    """Résumé of an item ✨ — returns JSON."""
    return {"id": item_id}


@app.route("/v1/weird path/{x}/:y/<z>")
def odd_path():
    return "ok"


@app.get("/v1/quote\"d")
def quoted():
    return "quoted"


def connect(token: str = "sk_test_FAKE_TOKEN_1234567890abcdef") -> str:
    """Default argument holds an obviously fake secret 🔑."""
    return token
