# Authentication & User Management

This document explains the authentication system added to the Newk's Reporting Platform.

## Overview

The app now supports:
- **User Registration** - Users can create accounts with email and password
- **User Login** - Secure JWT-based authentication
- **User Preferences** - Per-user settings (default locations, etc.)
- **Protected Endpoints** - Saved reports are now user-specific

## Backend Setup

### 1. Install Dependencies

```bash
cd backend
pip install -r requirements.txt
```

New dependencies added:
- `python-jose[cryptography]` - JWT token handling
- `passlib[bcrypt]` - Password hashing
- `python-multipart` - Form data handling

### 2. Environment Variables

Add to your `.env` file:

```bash
JWT_SECRET_KEY=your-super-secret-key-change-this-in-production
```

**Important:** Generate a strong secret key for production:
```python
import secrets
print(secrets.token_urlsafe(32))
```

## API Endpoints

### Authentication

#### `POST /api/auth/register`
Register a new user.

**Request:**
```json
{
  "email": "user@example.com",
  "password": "securepassword123",
  "name": "John Doe"
}
```

**Response:**
```json
{
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
  "token_type": "bearer",
  "user": {
    "id": "uuid",
    "email": "user@example.com",
    "name": "John Doe",
    "createdAt": "2025-01-01T00:00:00",
    "preferences": {}
  }
}
```

#### `POST /api/auth/login`
Login with email and password.

**Request:**
```json
{
  "email": "user@example.com",
  "password": "securepassword123"
}
```

**Response:** Same as register.

#### `GET /api/auth/me`
Get current user information. Requires authentication.

**Headers:**
```
Authorization: Bearer <token>
```

#### `PUT /api/auth/me`
Update user information or preferences. Requires authentication.

**Request:**
```json
{
  "name": "Updated Name",
  "preferences": {
    "defaultLocations": ["location-id-1", "location-id-2"],
    "defaultDateRange": "last_7_days"
  }
}
```

## User Storage

Users are stored in `backend/users.json`. This file is created automatically when the first user registers.

**Security Note:** Passwords are hashed using bcrypt before storage. Never store plain text passwords.

## Protected Endpoints

The following endpoints now require authentication:
- `GET /api/saved-reports` - Only returns reports created by the current user
- `POST /api/saved-reports` - Creates reports associated with the current user
- `PUT /api/saved-reports/{id}` - Only allows updating own reports
- `DELETE /api/saved-reports/{id}` - Only allows deleting own reports

## Frontend Integration

To use authentication in the frontend:

1. **Store the token** after login/register:
```javascript
localStorage.setItem('auth_token', response.access_token);
localStorage.setItem('user', JSON.stringify(response.user));
```

2. **Include token in API requests**:
```javascript
fetch('/api/saved-reports', {
  headers: {
    'Authorization': `Bearer ${localStorage.getItem('auth_token')}`,
    'Content-Type': 'application/json'
  }
});
```

3. **Check authentication status**:
```javascript
const token = localStorage.getItem('auth_token');
if (!token) {
  // Redirect to login
}
```

## Next Steps

1. **Add Login/Register UI** to the frontend
2. **Protect frontend routes** - Show login page if not authenticated
3. **Add user preferences UI** - Let users save default settings
4. **Optional: Password reset** - Add forgot password functionality
5. **Optional: Email verification** - Verify emails before allowing login

## Migration Notes

- Existing saved reports with `createdBy: "System"` will need to be migrated
- Users will need to register new accounts
- Consider adding an admin user creation script for initial setup

