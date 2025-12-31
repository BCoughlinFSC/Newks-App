# Super Admin Setup Guide

## How Super Admin Login Works

The super admin account is created automatically when you configure it in your `.env` file. The super admin can log in just like any other user, but has additional privileges.

## Setup Instructions

### 1. Add to `.env` file

Add these three environment variables to your `backend/.env` file:

```bash
SUPER_ADMIN_EMAIL=admin@newks.com
SUPER_ADMIN_PASSWORD=your-secure-password-here
SUPER_ADMIN_NAME=Super Admin
```

**Important:**
- Use a strong password (at least 6 characters, max 72 bytes)
- The email will be converted to lowercase automatically
- The password will be hashed and stored securely

### 2. Restart the Backend

After adding the environment variables, restart your backend server. The super admin account will be created automatically on startup.

### 3. Login

The super admin logs in the same way as regular users:
1. Open the app
2. Click "Login" (or it will show automatically)
3. Enter the super admin email and password
4. You'll be logged in with admin privileges

## Admin Features

Once logged in as super admin, you have access to:

### Admin API Endpoints

1. **List All Users** - `GET /api/admin/users`
   - View all registered users
   - Requires admin authentication

2. **Create User** - `POST /api/admin/users`
   - Create new user accounts
   - Set email, name, and password
   - Requires admin authentication

3. **Update User Role** - `PUT /api/admin/users/{user_id}/role`
   - Promote users to admin
   - Demote admins to regular users
   - Requires admin authentication

## Admin vs Regular Users

- **Regular Users (`role: "user"`)**:
  - Can create and manage their own saved reports
  - Can run reports
  - Cannot see other users' reports
  - Cannot manage other users

- **Admin Users (`role: "admin"`)**:
  - All regular user capabilities
  - Can view all users
  - Can create new user accounts
  - Can change user roles (promote to admin, etc.)
  - Can access admin-only endpoints

## Security Notes

1. **Password Storage**: All passwords are hashed using bcrypt before storage
2. **JWT Tokens**: Admin status is included in the JWT token
3. **Role Verification**: Admin endpoints verify the user's role before allowing access
4. **Environment Variables**: Keep your `.env` file secure and never commit it to version control

## Troubleshooting

### Super Admin Not Created

- Check that all three environment variables are set in `.env`
- Check backend logs for any errors during initialization
- Verify the email format is correct
- Ensure password meets requirements (6+ characters, max 72 bytes)

### Can't Access Admin Endpoints

- Verify you're logged in as an admin (check `/api/auth/me` - should show `"role": "admin"`)
- Check that your JWT token is included in the Authorization header
- Verify the endpoint requires admin access (should return 403 if not admin)

### Forgot Super Admin Password

1. Update `SUPER_ADMIN_PASSWORD` in `.env`
2. Restart the backend
3. The password will be updated automatically

## Example: Creating Additional Admins

Once logged in as super admin, you can promote other users to admin:

```bash
# Get your auth token first (from login)
TOKEN="your-jwt-token-here"

# List all users to find the user ID
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/admin/users

# Promote a user to admin (replace USER_ID)
curl -X PUT \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"role": "admin"}' \
  http://localhost:8000/api/admin/users/USER_ID/role
```

Or use the API docs at `http://localhost:8000/docs` to test the endpoints interactively.

