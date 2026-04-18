from fastapi import FastAPI

app = FastAPI(title="Supermarket Price Tracker API")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/retailers")
def retailers():
    return [{"key":"tesco"},{"key":"morrisons"},{"key":"ocado"}]

@app.get("/search")
def search(q: str | None = None, retailer: str | None = None):
    return []

@app.post("/track")
def track(payload: dict):
    return {"message": "tracking updated", "payload": payload}

@app.get("/history/{id}")
def history(id: int):
    return {"product_id": id, "history": []}
