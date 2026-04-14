import requests

url = "https://graph.facebook.com/v22.0/984411908097951/messages"

headers = {
    "Authorization": "Bearer EAAe8YaGfcZBEBROH4mZBu1Q3agMlxIfpSoWQvFQ0kkDBpG3KEUzLjSPMieZBRIDHSLbhLVxn9POxfDEhzJGWTZB1G8qz66ugiYsuzOQ0QhzjqdzWhC6cQtfrPwQrSazThPLrMPOdKkvsKSga35kfy5p5YM3T13muiafB5CVqtZC0W6UjuCijCdlnZAXELowRrJZCbvc6PQz4XLLVht65TxOub9lGzVY87m4OlT19PZAcX9gZCAPsUkaTBty9wBfTwZCts3AulMBqDuubVURkLETNRET3f7",
    "Content-Type": "application/json"
}

data = {
    "messaging_product": "whatsapp",
    "to": "917736094292",
    "type": "template",
    "template": {
        "name": "hello_world",
        "language": {"code": "en_US"}
    }
}

response = requests.post(url, headers=headers, json=data)
print(response.json())