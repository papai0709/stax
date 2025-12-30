# Swagger API Documentation

This project now includes interactive Swagger/OpenAPI documentation for all API endpoints.

## Accessing the Documentation

### Enhanced Story API (Port 8080)
Once you start the enhanced story API server:
```bash
python3 -m src.api_enhanced
```

Access the Swagger UI at:
- **Swagger UI**: http://localhost:8080/api/docs
- **OpenAPI Spec**: http://localhost:8080/apispec.json

### Monitor API (Port 5001)
Once you start the monitor API server:
```bash
python -m src.monitor_api_complete
```

Access the Swagger UI at:
- **Swagger UI**: http://localhost:5001/api/docs
- **OpenAPI Spec**: http://localhost:5001/apispec.json

## Features

The Swagger UI provides:
- **Interactive Testing**: Try out API endpoints directly from the browser
- **Request/Response Examples**: See example payloads and responses
- **Schema Documentation**: View all request and response schemas
- **Authentication Testing**: Test authenticated endpoints
- **Export Options**: Download the OpenAPI specification

## Available API Endpoints

### Enhanced Story API
- `GET /` - Health check endpoint
- `POST /api/stories/enhanced/auto` - Create enhanced story with complexity analysis

### Monitor API
- `GET /` - Dashboard page
- `GET /api/health` - Health check
- `POST /api/monitor/start` - Start monitoring service
- `POST /api/monitor/stop` - Stop monitoring service
- `GET /api/monitor/status` - Get monitor status
- `GET /api/epics` - Get all monitored epics
- `GET /api/stats` - Get statistics
- `GET /api/config` - Get configuration
- `PUT /api/config` - Update configuration
- `POST /api/monitor/check` - Manual check
- `POST /api/test-cases/extract` - Extract test cases
- `POST /api/test-cases/preview` - Preview test cases
- `POST /api/test-cases/bulk-extract` - Bulk extract test cases
- `POST /api/stories/extract` - Extract stories
- `POST /api/stories/preview` - Preview stories
- `POST /api/stories/<story_id>/test-cases` - Extract test cases for story
- `GET /api/logs` - Get logs
- `POST /api/logs/clear` - Clear logs
- `POST /api/stories/<story_id>/test-cases/upload` - Upload test cases

## Installation

The required dependency has been added to `requirements.txt`:
```bash
pip install -r requirements.txt
```

## Using the Swagger UI

1. **Navigate** to the Swagger UI URL
2. **Browse** available endpoints organized by tags
3. **Click** on any endpoint to expand details
4. **Try it out** - Click the "Try it out" button
5. **Fill parameters** - Enter required parameters
6. **Execute** - Click "Execute" to make the API call
7. **View response** - See the actual response from the server

## API Authentication

If your APIs require authentication (e.g., API keys, tokens):
1. Click the "Authorize" button at the top of the Swagger UI
2. Enter your authentication credentials
3. The credentials will be included in subsequent requests

## Customization

To customize the Swagger documentation:
- Edit the `swagger_template` in the API files
- Add more detailed descriptions to endpoint docstrings
- Include more response examples
- Add security definitions for authentication

## OpenAPI Specification

The OpenAPI 2.0 (Swagger) specification can be downloaded from:
- `/apispec.json` endpoint

You can use this specification to:
- Generate client libraries in various languages
- Import into API testing tools (Postman, Insomnia)
- Generate documentation in other formats
- Validate API contracts

## Troubleshooting

### Swagger UI not loading
- Ensure the server is running
- Check that the port is not blocked by firewall
- Verify no other service is using the same port

### Endpoints not showing
- Check that routes are properly decorated with docstrings
- Ensure the YAML format in docstrings is valid
- Review server logs for any errors

### Authentication issues
- Verify credentials are correct
- Check that authentication headers are properly configured
- Ensure the API supports the authentication method being used
