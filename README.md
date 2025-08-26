# Stellar API

A FastAPI application for handling authentication and API requests with Cloudflare protection bypass.

## Setup

1. Clone the repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Create a `.env` file with your credentials (use `.env.example` as a template)
4. Run the application:
   ```bash
   uvicorn main:app --reload
   ```

## Environment Variables

Create a `.env` file with the following variables:

```
TWOCAPTCHA_API_KEY=your_2captcha_api_key
WELLNESSLIVING_CLIENT_ID=your_client_id
WELLNESSLIVING_CLIENT_SECRET=your_client_secret
```

## API Endpoints

- `GET /get-token` - Get authentication token

## Development

- Python 3.8+
- FastAPI
- Selenium for browser automation
- 2Captcha for solving Cloudflare challenges
