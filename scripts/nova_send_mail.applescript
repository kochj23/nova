#!/usr/bin/osascript
-- nova_send_mail.applescript
-- Sends an email from nova@digitalnoise.net via macOS Mail.
-- Usage: osascript nova_send_mail.applescript "to@address.com" "Subject Line" "Body text here"
-- Jordan Koch -- 2026

on run argv
    if (count of argv) < 3 then
        return "ERROR: Usage: osascript nova_send_mail.applescript <to> <subject> <body>"
    end if

    set toAddr to item 1 of argv
    set msgSubject to item 2 of argv
    set msgBody to item 3 of argv
    set fromAccount to "nova@digitalnoise.net"

    tell application "Mail"
        set outMsg to make new outgoing message with properties {sender:fromAccount, subject:msgSubject, content:msgBody, visible:false}
        tell outMsg
            make new to recipient at end of to recipients with properties {address:toAddr}
        end tell
        send outMsg
    end tell

    return "SENT: Email to " & toAddr & " - " & msgSubject
end run
