import asyncio
import requests
import json

async def test_fetch():
    url = "https://api.stripe.com/v1/3ds2/authenticate"
    headers = {"authority": "api.stripe.com", "accept": "application/json", "content-type": "application/x-www-form-urlencoded"}
    
    # Try sending app as a JSON string
    app_str = json.dumps({"sdk_trans_id": "b5972ebf-3e7c-4ad5-91af-28cd673883ca", "device_render_options": {"sdk_interface": "03", "sdk_ui_type": ["01", "02", "03", "04", "05"]}}, separators=(",", ":"))
    data = {
        "key": "pk_live_51I2N5tK1R9Q2sW8E4L3mZ9Q4u",
        "source": "src_123",
        "app": app_str,
        "browser": json.dumps({"color_depth":"32"}, separators=(",", ":"))
    }
    response = requests.post(url, headers=headers, data=data, timeout=15)
    print("Test 1 (app as json string):", response.json())
    
    # Try sending app as flat dict
    data2 = {
        "key": "pk_live_51I2N5tK1R9Q2sW8E4L3mZ9Q4u",
        "source": "src_123",
        "app[sdk_trans_id]": "b5972ebf-3e7c-4ad5-91af-28cd673883ca",
        "app[device_render_options][sdk_interface]": "03",
        "app[device_render_options][sdk_ui_type][0]": "01",
        "browser": json.dumps({"color_depth":"32"}, separators=(",", ":"))
    }
    response2 = requests.post(url, headers=headers, data=data2, timeout=15)
    print("Test 2 (app as flat keys):", response2.json())

asyncio.run(test_fetch())
