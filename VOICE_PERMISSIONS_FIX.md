# Discord Voice Connection Error 4006 - Fix Guide

## 🚨 Problem
You're getting error code 4006 when trying to connect to voice channels:
```
discord.errors.ConnectionClosed: Shard ID None WebSocket closed with 4006
```

## 🔍 Root Cause
Error code 4006 means the bot doesn't have the required permissions to connect to voice channels. This is a Discord API permission issue.

## ✅ Solution

### 1. Check Bot Permissions in Discord

**Go to your Discord server settings and verify the bot has these permissions:**

#### Required Permissions:
- ✅ **Connect** - Join voice channels
- ✅ **Speak** - Transmit audio in voice channels  
- ✅ **Use Voice Activity** - Use voice activation
- ✅ **View Channel** - See voice channels
- ✅ **Manage Channels** - Create/delete voice channels
- ✅ **Send Messages** - Send messages in text channels
- ✅ **Read Message History** - Read previous messages

#### How to Check:
1. Go to your Discord server
2. Right-click the bot user → "Server Permissions"
3. Or go to Server Settings → Roles → Bot Role
4. Verify all the above permissions are enabled

### 2. Check Channel-Specific Permissions

**The bot might have server permissions but be denied at the channel level:**

1. Go to the voice channel you're trying to connect to
2. Right-click the channel → "Edit Channel"
3. Go to "Permissions" tab
4. Check if the bot role is explicitly denied any permissions
5. Make sure the bot role has "Connect" and "Speak" permissions

### 3. Check Bot Role Hierarchy

**The bot's role must be higher than the channels it's trying to manage:**

1. Go to Server Settings → Roles
2. Make sure the bot's role is positioned **above** any roles that might deny permissions
3. The bot role should be above the "everyone" role

### 4. Test with a Simple Voice Command

**Try this basic test to isolate the issue:**

```bash
# Test if the bot can join any voice channel
/golive "Test Album" -- This should create a new voice channel
```

If this works, the issue is with specific channel permissions.
If this fails, the issue is with server-wide bot permissions.

### 5. Bot Invite Link Permissions

**If you need to re-invite the bot, use this link with proper permissions:**

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_BOT_ID&permissions=3148800&scope=bot%20applications.commands
```

**Required Permission Bits:**
- `3148800` includes: Connect, Speak, Use Voice Activity, View Channels, Manage Channels, Send Messages, Read Message History

### 6. Alternative: Use Bot Permissions Calculator

1. Go to: https://discordapi.com/permissions.html
2. Select these permissions:
   - ✅ Connect
   - ✅ Speak  
   - ✅ Use Voice Activity
   - ✅ View Channels
   - ✅ Manage Channels
   - ✅ Send Messages
   - ✅ Read Message History
   - ✅ Add Reactions
3. Copy the generated invite link

## 🔧 Quick Fix Steps

### Step 1: Check Current Permissions
```bash
# Run this command in Discord to see bot permissions
/debug status
```

### Step 2: Verify Bot Role
1. Go to Server Settings → Roles
2. Find your bot's role
3. Make sure it has all required permissions
4. Ensure it's positioned high enough in the role hierarchy

### Step 3: Test Voice Connection
```bash
# Try creating a new voice channel
/golive "Test Album"
```

### Step 4: Check Logs
If it still fails, check the bot logs for more specific error messages.

## 🚨 Common Issues

### Issue 1: Bot Role Too Low
**Problem:** Bot role is below roles that deny permissions
**Solution:** Move bot role higher in the role hierarchy

### Issue 2: Channel-Specific Denials
**Problem:** Bot is denied permissions on specific channels
**Solution:** Check channel permissions and remove denials

### Issue 3: Missing Server Permissions
**Problem:** Bot wasn't invited with proper permissions
**Solution:** Re-invite bot with correct permission bits

### Issue 4: Discord API Issues
**Problem:** Temporary Discord API problems
**Solution:** Wait a few minutes and try again

## 📋 Permission Checklist

Before testing voice features, verify:

- [ ] Bot has "Connect" permission
- [ ] Bot has "Speak" permission  
- [ ] Bot has "Use Voice Activity" permission
- [ ] Bot has "View Channel" permission
- [ ] Bot has "Manage Channels" permission
- [ ] Bot role is high enough in hierarchy
- [ ] No channel-specific permission denials
- [ ] Bot was invited with proper permissions

## 🎯 Test Commands

After fixing permissions, test these commands:

1. **Basic Test:**
   ```
   /help
   /ping
   ```

2. **Voice Test:**
   ```
   /golive "Test Album"
   ```

3. **Debug Test:**
   ```
   /debug status
   /debug config
   ```

## 📞 Still Having Issues?

If you're still getting error 4006 after checking all permissions:

1. **Check bot logs** for more specific error messages
2. **Try a different voice channel** to isolate the issue
3. **Restart the bot** to clear any cached connection states
4. **Check Discord status** at https://status.discord.com/

**The bot's improved error handling in v2.0 should provide better error messages to help diagnose the issue.**
