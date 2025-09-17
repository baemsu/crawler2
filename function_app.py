import azure.functions as func

app = func.FunctionApp()

@app.route(route="hello", auth_level=func.AuthLevel.ANONYMOUS)
def hello(req: func.HttpRequest) -> func.HttpResponse:
    name = req.params.get("name") or "world"
    return func.HttpResponse(f"Hello, {name}!")
