import datetime
import json
import os
import pytz

from boto3 import Session
from flask import Flask, request, jsonify


app = Flask(__name__)
app.s3_session = Session()
MY_BUCKET = os.environ["STORE_BUCKET"]


@app.route("/health")
def healthcheck():
    return jsonify({"status": "ok"})


@app.route("/keys/<cache_key>", methods=["GET"])
def get_key(cache_key):
    s3_client = app.s3_session.client("s3")
    result = None
    try:
        response = s3_client.get_object(
            Bucket=MY_BUCKET,
            Key=cache_key,
        )
        if response["Expires"] >= pytz.utc.localize(datetime.datetime.utcnow()):
            result = json.loads(response["Body"].read())
    except Exception as e:
        print(e)
    return jsonify(result)


@app.route("/keys/<cache_key>", methods=["PUT"])
def put_key_data(cache_key):
    req_body = json.loads(request.data)

    key_data = req_body["data"]
    expiration_date_str = req_body["expiration_date"]
    expiration_date = datetime.datetime.fromisoformat(expiration_date_str)

    s3_client = app.s3_session.client("s3")

    _ = s3_client.put_object(
        Bucket=MY_BUCKET,
        Key=cache_key,
        Body=json.dumps(key_data),
        Expires=expiration_date,
    )

    return jsonify({"message": f"key data for {cache_key} stored successfully."})


if __name__ == "__main__":
    app.run("0.0.0.0", port=5000)
