import datetime
import json
import os


from boto3 import Session
from flask import Flask, request, jsonify
from redis import StrictRedis
from cache_ring_management import CacheRingManager


APP_PORT = 5000

REDIS_IP = os.environ["REDIS_ADDRESS"]
MY_BUCKET = os.environ["STORE_BUCKET"]

redis_client = StrictRedis(host=REDIS_IP)

app = Flask(__name__)
app.aws_session = Session()
app.my_ip = os.environ["NODE_IP"]
app.cache_manager = CacheRingManager(
    ip=app.my_ip,
    port=APP_PORT,
    redis_client=redis_client,
    nodes_list_key="nodes_list",
    heartbeat_timeout=100,
    s3_bucket=MY_BUCKET,
    s3_client=app.aws_session.client("s3"),
)


@app.route("/health")
def healthcheck():
    app.cache_manager.set_heartbeat()
    return jsonify({"status": "ok"})


@app.route("/keys/<cache_key>", methods=["GET"])
def get_key(cache_key):
    result = app.cache_manager.get_cache_value(cache_key)
    return jsonify(result)


@app.route("/keys/<cache_key>", methods=["PUT"])
def put_key_data(cache_key):
    req_body = json.loads(request.data)

    key_data = req_body["data"]
    expiration_date_str = req_body["expiration_date"]
    expiration_date = datetime.datetime.fromisoformat(expiration_date_str)

    app.cache_manager.set_cache_value(
        key=cache_key, value=key_data, expiration_date=expiration_date
    )

    return jsonify({"message": f"key data for {cache_key} stored successfully."})


@app.route("/internal/keys/<cache_key>", methods=["GET"])
def get_key_directly(cache_key):
    result = app.cache_manager.get_cache_value(key=cache_key, local_only=True)
    return jsonify(result)


@app.route("/internal/keys/<cache_key>", methods=["PUT"])
def put_key_directly(cache_key):
    req_body = json.loads(request.data)
    key_data = req_body["data"]
    expiration_date_str = req_body["expiration_date"]
    expiration_date = datetime.datetime.fromisoformat(expiration_date_str)

    app.cache_manager.set_cache_value(
        key=cache_key, value=key_data, expiration_date=expiration_date, local_only=True
    )


@app.route("/internal/refresh", methods=["POST"])
def refresh_cache():
    app.cache_manager.refresh_cache()
    return jsonify({"status": "ok"})


# DEBUG METHOD


@app.route("/internal/nodes", methods=["GET"])
def get_all_nodes():
    result = app.cache_manager.get_live_nodes()
    return jsonify(result)


if __name__ == "__main__":
    app.run("0.0.0.0", port=APP_PORT)
