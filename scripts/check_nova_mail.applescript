#!/usr/bin/osascript
-- check_nova_mail.applescript
-- Checks nova@digitalnoise.net INBOX for unread messages from unknown senders
-- Returns a summary string that Nova can read and act on
-- Jordan Koch — 2026

set novaAccount to "nova@digitalnoise.net"
set knownSenders to {"kochj23@gmail.com", "kochj@digitalnoise.net", "mjramos76@gmail.com", "jason.cox@disney.com", "James.Tatum@disney.com", "amy.mccain@gmail.com", "sam@jasonacox.com", "marey@makehorses.org"}
set output to ""
set foundCount to 0

tell application "Mail"
    set novaInbox to mailbox "INBOX" of account novaAccount
    set allMessages to messages of novaInbox
    
    repeat with m in allMessages
        if read status of m is false then
            set theSender to sender of m
            set isKnown to false
            
            repeat with known in knownSenders
                if theSender contains known then
                    set isKnown to true
                    exit repeat
                end if
            end repeat
            
            if not isKnown then
                set foundCount to foundCount + 1
                set theSubject to subject of m
                set theDate to date received of m
                set output to output & "FROM: " & theSender & "\nSUBJECT: " & theSubject & "\nDATE: " & (theDate as string) & "\n---\n"
            end if
        end if
    end repeat
end tell

if foundCount > 0 then
    return "NEW_MAIL|" & foundCount & " new message(s) to Nova from unknown senders:\n" & output
else
    return "NO_NEW_MAIL"
end if
