# Standard library imports
import json
import re
import time
import warnings
from email.utils import parsedate_to_datetime
import base64
import hashlib
import os

# Third-party imports
import flask
from flask_cors import CORS
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Disable SSL warnings
warnings.filterwarnings('ignore', message='Unverified HTTPS request')

# Flask app initialization
app = flask.Flask(__name__)
CORS(app)

# Constants
GROK_API_URL = "https://grok.x.com/2/grok/add_response.json"
MODELS = [
    {
        "id": "grok-3",
        "object": "model",
        "created": 1145141919,
        "owned_by": "yilongma"
    },
    {
        "id": "grok-3t",
        "object": "model",
        "created": 1145141919,
        "owned_by": "yilongma"
    },
    {
        "id": "grok-3ds",
        "object": "model",
        "created": 1145141919,
        "owned_by": "yilongma"
    }
]

# Retry configuration
retry_strategy = Retry(
    total=5,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"]
)

# Configure SSL adapter
adapter = HTTPAdapter(
    max_retries=retry_strategy
)

session = requests.Session()
session.mount("https://", adapter)
session.verify = False  # Disable SSL verification
session.timeout = (10, 30)  # Set connection and read timeout

# Conversation history store (in-memory for simplicity, consider persistent storage for production)
conversations = {}


def convert_tweet_links(message):
    # Match [link](#tweet=number) format
    pattern = r'\[link\]\(#tweet=(\d+)\)'
    # Replace with [link](https://x.com/elonmusk/status/number)
    return re.sub(pattern, r'[link](https://x.com/elonmusk/status/\1)', message)

def encode_chat_id(grok_id):
    """Convert Grok ID to OpenAI format ID"""
    # Use SHA256 to generate a fixed-length hash
    hash_obj = hashlib.sha256(str(grok_id).encode())
    # Take the first 24 bytes and convert to base64
    b64_str = base64.b64encode(hash_obj.digest()[:24]).decode()
    # Replace special characters
    b64_str = b64_str.replace('+', 'x').replace('/', 'y').replace('=', 'z')
    return f"chatcmpl-{b64_str[:32]}"

def decode_chat_id(openai_id):
    """Try to extract the original Grok ID from the OpenAI format ID"""
    try:
        # Remove prefix
        b64_str = openai_id.replace("chatcmpl-", "")
        # Restore special characters
        b64_str = b64_str.replace('x', '+').replace('y', '/').replace('z', '=')
        # Base64 decode
        decoded = base64.b64decode(b64_str + "==")
        # Return as an integer
        return int.from_bytes(decoded[:8], 'big')
    except:
        return None


@app.before_request
def before_request():
    # 如果是POST请求到特定端点，而且内容类型不是application/json，尝试修改它
    if flask.request.path == '/v1/chat/completions' and flask.request.method == 'POST':
        flask.request.environ['CONTENT_TYPE'] = 'application/json'

@app.route('/v1/models', methods=['GET'])
def get_model():
    return flask.jsonify({"object": "list", "data": MODELS})

# 添加OPTIONS请求处理
@app.route('/v1/chat/completions', methods=['OPTIONS'])
def handle_options():
    response = flask.Response()
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'POST,OPTIONS')
    return response

# 修改主处理函数开头部分
@app.route('/v1/chat/completions', methods=['POST'])
def openai_to_grok_proxy():
    # 尝试以更宽松的方式获取JSON数据
    try:
        # 1. 首先尝试原始 JSON 解析
        if flask.request.is_json:
            openai_request_data = flask.request.get_json()
        # 2. 尝试从请求体解析 JSON
        else:
            try:
                openai_request_data = json.loads(flask.request.data.decode('utf-8'))
            except:
                # 3. 如果还不行，尝试从表单数据提取
                form_data = flask.request.form.get('data') 
                if form_data:
                    openai_request_data = json.loads(form_data)
                else:
                    return "无法解析请求数据", 400
    except Exception as e:
        return f"请求体无效: {str(e)}", 400
    # Print input request
    print("\n=== Incoming OpenAI Format Request ===")
    print("Headers:", json.dumps(dict(flask.request.headers), indent=2))
    print("Body:", json.dumps(flask.request.get_json(), indent=2))
    print("=====================================\n")
    
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

    # Get the conversation ID if present, otherwise generate a new one
    conversation_id = openai_request_data.get('conversation_id', str(int(time.time() * 1000)))

    # Retrieve or initialize conversation history
    if conversation_id not in conversations:
        conversations[conversation_id] = []
        print(f"New conversation started: {conversation_id}")
    else:
        print(f"Continuing conversation: {conversation_id}")

    # Append new messages to the conversation history
    conversations[conversation_id].extend(messages)

    grok_request_headers = {
        'authorization': f'Bearer {auth_bearer}',
        'content-type': 'application/json; charset=UTF-8',  # Important: Set to application/json
        'accept-encoding': 'gzip, deflate, br, zstd',
        'cookie': f'auth_token={auth_token}'
    }

     # Define mapping
    SENDER_TO_ROLE = {
        1: "user",
        2: "assistant"
    }

    ROLE_TO_SENDER = {
        "user": 1,
        "assistant": 2
    }


    # Prepare the request body for Grok
    grok_request_body = {
        "responses": [],
        "grokModelOptionId": "grok-3", #default model
        "isDeepsearch": False,
        "isReasoning": False
    }

    # Check if the requested model is grok-3t
    if openai_request_data.get('model') == 'grok-3t':
        grok_request_body['isReasoning'] = True
          
    # Check if the requested model is grok-3ds
    if openai_request_data.get('model') == 'grok-3ds':
        grok_request_body['isDeepsearch'] = True

    # Construct Grok's 'responses' from the entire conversation history
    for message in conversations[conversation_id]:
        grok_request_body['responses'].append({
            "message": message['content'],
            "sender": ROLE_TO_SENDER.get(message['role'], 1),  # Default to user
            "fileAttachments": []
        })


    def stream_grok_response():
        try:
            grok_response = session.post(
                GROK_API_URL,
                headers=grok_request_headers,
                json=grok_request_body,
                stream=True
            )
            grok_response.raise_for_status()

            date_str = grok_response.headers.get('date')
            if date_str:
                dt = parsedate_to_datetime(date_str)
                openai_created_time = int(dt.timestamp())
            else:
                openai_created_time = int(time.time())

            grok_id = grok_response.headers.get('userChatItemId') or int(time.time() * 1000)
            openai_chunk_id = encode_chat_id(grok_id)
            openai_model = grok_request_body['grokModelOptionId']

            # 追踪是否已经发送了停止信号
            stop_signal_sent = False

            for line in grok_response.iter_lines():
                if line:
                    try:
                        grok_data = json.loads(line.decode('utf-8'))
                        if 'result' in grok_data:
                            result = grok_data['result']
                            if 'sender' in result:
                                role = SENDER_TO_ROLE.get(result['sender'], 'assistant')
                                message_content = convert_tweet_links(result.get('message', ''))

                                is_thinking = result.get('isThinking', False)  # Check for thinking status
                                if is_thinking:
                                    message_content = "<think>\n" + message_content + "\n</think>\n\n" # Wrap thinking tokens


                                openai_chunk = {
                                    "id": openai_chunk_id,
                                    "choices": [{
                                        "index": 0,
                                        "delta": {"role": role, "content": message_content},
                                        "logprobs": None,
                                        "finish_reason": None
                                    }],
                                    "created": openai_created_time,
                                    "model": openai_model,
                                    "object": "chat.completion.chunk"
                                }
                                yield f"data: {json.dumps(openai_chunk)}\n\n"

                                # Append assistant's response to conversation history
                                if role == 'assistant':
                                      conversations[conversation_id].append({
                                          "role": "assistant",
                                          "content": message_content
                                    })

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
                            stop_signal_sent = True

                    except json.JSONDecodeError:
                        print(f"Warning: Could not decode JSON: {line.decode('utf-8')}")
                    except Exception as e:
                        print(f"Error processing Grok response chunk: {e}")

            # 如果没有发送过stop信号，确保在结束前发送一个
            if not stop_signal_sent:
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

            yield "data: [DONE]\n\n"

        except requests.exceptions.RequestException as e:
            error_message = f"Grok API request failed: {e}"
            print(error_message)
            openai_error_chunk = {
                "error": {
                    "message": error_message,
                    "type": "api_error",
                    "param": None,
                    "code": None
                }
            }
            yield f"data: {json.dumps(openai_error_chunk)}\n\n"
            yield "data: [DONE]\n\n"


    response = flask.Response(
        flask.stream_with_context(stream_grok_response()), 
        mimetype='text/event-stream'
    )
    
    # 添加必要的SSE响应头
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Connection'] = 'keep-alive'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Content-Type'] = 'text/event-stream'
    
    return response

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))