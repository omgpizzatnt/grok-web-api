import flask
import requests
import json

app = flask.Flask(__name__)

GROK_API_URL = "https://grok.x.com/2/grok/add_response.json"

@app.route('/v1/chat/completions', methods=['POST'])
def openai_to_grok_proxy():
    auth_header = flask.request.headers.get('authorization')
    if not auth_header:
        return "Authorization header is missing", 401

    try:
        auth_bearer, auth_token = auth_header.split("Bearer ")[1].split(",")
    except ValueError:
        return "Invalid Authorization header format. Expected 'Bearer $AUTH_BEARER,$AUTH_TOKEN'", 400

    openai_request_data = flask.request.get_json()
    if not openai_request_data or 'messages' not in openai_request_data:
        return "Invalid request body. Expected 'messages' in request body", 400

    messages = openai_request_data['messages']
    if not messages:
        return "'messages' cannot be empty", 400

    last_user_message = None
    for message in reversed(messages):
        if message['role'] == 'user':
            last_user_message = message['content']
            break

    if not last_user_message:
        return "No 'user' message found in messages", 400

    grok_request_headers = {
        'authorization': f'Bearer {auth_bearer}',
        'content-type': 'application/json; charset=UTF-8', # Important: Set to application/json
        'accept-encoding': 'gzip, deflate, br, zstd',
        'cookie': f'auth_token={auth_token}'
    }

    grok_request_body = {
        "responses": [
            {
                "message": last_user_message,
                "sender": 1, # Assuming sender '1' represents user for Grok API
                "fileAttachments": []
            }
        ]
    }

    def generate():
        try:
            with requests.post(GROK_API_URL, headers=grok_request_headers, json=grok_request_body, stream=True) as grok_response: # Use json= to send json body
                grok_response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

                openai_chunk_id = "chatcmpl-xxxxxxxxxxxxxxxxxxxxxxxx" # Generate a unique ID if needed
                openai_created_time = 1737903811 # Use current timestamp if needed
                openai_model = "grok-1" # Or map from openai_request_data.get('model') if needed

                for line in grok_response.iter_lines():
                    if line: # filter out keep-alive new lines
                        try:
                            grok_data = json.loads(line.decode('utf-8')) # Decode bytes to string then parse JSON
                            if 'result' in grok_data and 'sender' in grok_data['result'] and grok_data['result']['sender'] == 'ASSISTANT':
                                message_content = grok_data['result'].get('message', '')
                                openai_chunk = {
                                    "id": openai_chunk_id,
                                    "choices": [{
                                        "index": 0,
                                        "delta": {"role": "assistant", "content": message_content},
                                        "logprobs": None,
                                        "finish_reason": None
                                    }],
                                    "created": openai_created_time,
                                    "model": openai_model,
                                    "object": "chat.completion.chunk"
                                }
                                yield f"data: {json.dumps(openai_chunk)}\n\n"
                            elif 'result' in grok_data and 'isSoftStop' in grok_data['result'] and grok_data['result']['isSoftStop'] is True:
                                openai_chunk_stop = {
                                    "id": openai_chunk_id,
                                    "choices": [{
                                        "index": 0,
                                        "delta": {},
                                        "logprobs": None,
                                        "finish_reason": "stop"
                                    }],
                                    "created": openai_created_time,
                                    "model": openai_model,
                                    "object": "chat.completion.chunk"
                                }
                                yield f"data: {json.dumps(openai_chunk_stop)}\n\n"


                        except json.JSONDecodeError:
                            print(f"Warning: Could not decode JSON: {line.decode('utf-8')}") # Log invalid JSON lines
                        except Exception as e:
                            print(f"Error processing Grok response chunk: {e}") # Log other exceptions during processing

                yield "data: [DONE]\n\n" # Signal stream completion

        except requests.exceptions.RequestException as e:
            error_message = f"Grok API request failed: {e}"
            print(error_message)
            openai_error_chunk = {
                "error": {
                    "message": error_message,
                    "type": "api_error", # Or other appropriate error type
                    "param": None,
                    "code": None # Or specific error code if available
                }
            }
            yield f"data: {json.dumps(openai_error_chunk)}\n\n"
            yield "data: [DONE]\n\n" # Still send DONE to close stream in case of error


    return flask.Response(flask.stream_with_context(generate()), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)