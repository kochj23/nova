#!/usr/bin/osascript
-- nova_mail_handler.applescript
-- Checks nova@digitalnoise.net INBOX for unread messages.
-- Unknown senders: auto-reply, mark as read.
-- All new messages: return structured data so Nova can post to Slack.
-- Uses a 6-minute recency window for known senders to avoid re-posting on each cron cycle.
-- Jordan Koch -- 2026

set novaAccount to "nova@digitalnoise.net"
set jordanEmail to "kochj23@gmail.com"
-- Known senders loaded from local config. Add your own addresses here.
set knownSenders to {"nova@digitalnoise.net", "sam@jasonacox.com", "marey@makehorses.org", "oc@mostlycopyandpaste.com", "rockbot@makehorses.org", "gaston@bluemoxon.com", "colette@pilatesmuse.co"}

set repliedCount to 0
set emailList to ""
set emailCount to 0

-- Threshold: 6 minutes ago (360 seconds), to match 5-min cron with buffer
set cutoffDate to (current date) - 360

tell application "Mail"
    set novaInbox to mailbox "INBOX" of account novaAccount
    set allMessages to messages of novaInbox

    repeat with m in allMessages
        set theDate to date received of m
        set theSender to sender of m
        set theSubject to subject of m

        -- Skip bounce/system messages — never auto-reply to these
        set isSystemMessage to false
        set senderLower to theSender
        if senderLower contains "mailer-daemon" or senderLower contains "postmaster" or senderLower contains "Mail Delivery" or theSubject contains "Delivery Status" or theSubject contains "Undeliverable" or theSubject contains "Mail delivery failed" then
            set isSystemMessage to true
        end if
        if isSystemMessage then
            -- Mark as read silently, never reply
            set read status of m to true
        end if
        if isSystemMessage then
        else

        -- Determine if sender is known
        set isKnown to false
        repeat with known in knownSenders
            if theSender contains known then
                set isKnown to true
                exit repeat
            end if
        end repeat

        -- Get body snippet (first 300 chars)
        set theBody to ""
        try
            set theBody to content of m
            if length of theBody > 1500 then
                set theBody to text 1 thru 1500 of theBody & "..."
            end if
        end try

        if isKnown then
            -- Known sender: capture if unread (primary) or arrived in last 6 minutes (secondary guard)
            if (read status of m is false) or (theDate >= cutoffDate) then
                set emailCount to emailCount + 1
                set emailList to emailList & "FROM: " & theSender & return
                set emailList to emailList & "SUBJECT: " & theSubject & return
                set emailList to emailList & "DATE: " & (theDate as string) & return
                set emailList to emailList & "BODY: " & theBody & return
                set emailList to emailList & "---" & return
                set read status of m to true
            end if
        else
            -- Unknown sender: always process if unread
            if read status of m is false then
                -- Extract reply-to address
                set replyAddr to theSender
                if replyAddr contains "<" then
                    set AppleScript's text item delimiters to "<"
                    set replyAddr to text item 2 of replyAddr
                    set AppleScript's text item delimiters to ">"
                    set replyAddr to text item 1 of replyAddr
                    set AppleScript's text item delimiters to ""
                end if

                -- Auto-reply
                set replyBody to "Hi,

Thank you for your message. I'm Nova, Jordan's AI assistant. I'll make sure Jordan sees your email.

Best,
Nova (Jordan Koch's AI Assistant)"

                set outMsg to make new outgoing message with properties {sender:"nova@digitalnoise.net", subject:"Re: " & theSubject, content:replyBody, visible:false}
                tell outMsg
                    make new to recipient at end of to recipients with properties {address:replyAddr}
                end tell
                send outMsg

                set repliedCount to repliedCount + 1
                set read status of m to true

                set emailCount to emailCount + 1
                set emailList to emailList & "FROM: " & theSender & " [AUTO-REPLIED]" & return
                set emailList to emailList & "SUBJECT: " & theSubject & return
                set emailList to emailList & "DATE: " & (theDate as string) & return
                set emailList to emailList & "BODY: " & theBody & return
                set emailList to emailList & "---" & return
            end if
        end if

        end if -- end isSystemMessage check
    end repeat
end tell

if emailCount > 0 then
    return "EMAILS:" & emailCount & return & emailList
else
    return "NO_ACTION: No new messages."
end if
