from fastapi import FastAPI, HTTPException
from acfinder_crop import search_by_crop
from acfinder_pest import search_by_pest
from acfinder_pesticide import search_by_pesticide

app = FastAPI(title="ACFinderBE API")

@app.get("/crop")
def crop(name: str):
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    items = search_by_crop(name)
    return {"count": len(items), "items": items}

@app.get("/pest")
def pest(name: str):
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    items = search_by_pest(name)
    return {"count": len(items), "items": items}

@app.get("/pesticide")
def pesticide(name: str):
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    items = search_by_pesticide(name)
    return {"count": len(items), "items": items}
