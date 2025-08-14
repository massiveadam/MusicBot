# Discord Bot Permissions Guide

## Server-Level Permissions Required

Your bot needs these permissions at the **server level** (in Server Settings > Integrations > Bots and Apps > Your Bot):

### Essential Permissions:
- ✅ **Manage Channels** - Required to create categories and channels
- ✅ **Manage Permissions** - Required to set channel permissions
- ✅ **Connect** - Required to join voice channels
- ✅ **Speak** - Required to play audio in voice channels
- ✅ **Use Voice Activity** - Required for voice activation
- ✅ **Send Messages** - Required to send messages in text channels
- ✅ **View Channels** - Required to see channels
- ✅ **Add Reactions** - Required for interactive buttons

### Optional but Recommended:
- ✅ **Manage Messages** - For cleaning up old messages
- ✅ **Embed Links** - For rich embeds
- ✅ **Attach Files** - For sharing files
- ✅ **Read Message History** - For accessing previous messages

## How to Check Current Permissions:

1. Go to your Discord server
2. Click the server name → **Server Settings**
3. Go to **Integrations** → **Bots and Apps**
4. Find your bot and click on it
5. Check the permissions list

## How to Fix Missing Permissions:

1. **If permissions are missing:**
   - Click on your bot in the Integrations list
   - Enable the missing permissions
   - Save changes

2. **If the bot role is missing permissions:**
   - Go to **Roles** in Server Settings
   - Find your bot's role
   - Enable the required permissions

3. **If the bot doesn't have a role:**
   - Go to **Integrations** → **Bots and Apps**
   - Click on your bot
   - Create a role for it with the required permissions

## Common Issues:

### Error 50001: Missing Access
- **Cause**: Bot lacks `Manage Channels` permission
- **Fix**: Enable `Manage Channels` in bot permissions

### Error 50013: Missing Permissions
- **Cause**: Bot role is below the target channel's role in hierarchy
- **Fix**: Move bot role higher in the role hierarchy

### Error 50001: Missing Access (Channel Creation)
- **Cause**: Bot can't create channels in the target category
- **Fix**: Ensure bot has `Manage Channels` and `Manage Permissions`

## Testing Permissions:

After updating permissions, test with:
```
/golive artist:Test Album:Test
```

If successful, you should see:
- A new category created
- A voice channel created
- A text channel created
- No permission errors in logs

## Troubleshooting:

1. **Restart the bot** after changing permissions
2. **Check role hierarchy** - bot role must be above channels it manages
3. **Verify bot is in the server** - check member list
4. **Check audit logs** - look for permission-related entries

## Current Bot Permissions in Code:

The bot requests these permissions when creating channels:
- `manage_channels=True`
- `manage_permissions=True` 
- `connect=True`
- `speak=True`
- `use_voice_activation=True`
- `send_messages=True`
- `view_channel=True`
