#!/usr/bin/osascript
-- nova_mail_summary.applescript
-- Reads all configured macOS Mail accounts and collects messages
-- received in the last 24 hours. Returns structured text for Nova to summarize.
-- Jordan Koch -- 2026

set cutoffDate to (current date) - (24 * 60 * 60)
set output to ""
set totalCount to 0

-- Accounts to skip (Nova's own outbound account, avoid recursion)
set skipAccounts to {"nova@digitalnoise.net"}

tell application "Mail"
    set allAccounts to every account

    repeat with acct in allAccounts
        set acctName to name of acct

        -- Skip Nova's own account
        set shouldSkip to false
        repeat with skipName in skipAccounts
            if acctName contains skipName then
                set shouldSkip to true
                exit repeat
            end if
        end repeat
        if shouldSkip then
            -- skip to next account
        else
            try
                set inboxMB to mailbox "INBOX" of acct
                set msgs to messages of inboxMB

                set acctOutput to ""
                set acctCount to 0

                repeat with m in msgs
                    try
                        set msgDate to date received of m
                        if msgDate >= cutoffDate then
                            set msgFrom to sender of m
                            set msgSubject to subject of m
                            set msgRead to read status of m

                            -- Get body snippet (first 400 chars)
                            set msgBody to ""
                            try
                                set msgBody to content of m
                                if length of msgBody > 400 then
                                    set msgBody to text 1 thru 400 of msgBody & "..."
                                end if
                            end try

                            set readFlag to ""
                            if msgRead is false then
                                set readFlag to " [UNREAD]"
                            end if

                            set acctOutput to acctOutput & "FROM: " & msgFrom & readFlag & return
                            set acctOutput to acctOutput & "SUBJECT: " & msgSubject & return
                            set acctOutput to acctOutput & "DATE: " & (msgDate as string) & return
                            set acctOutput to acctOutput & "BODY: " & msgBody & return
                            set acctOutput to acctOutput & "---" & return
                            set acctCount to acctCount + 1
                        end if
                    end try
                end repeat

                if acctCount > 0 then
                    -- Get primary email address for this account
                    set acctEmail to acctName
                    try
                        set addrList to email addresses of acct
                        if (count of addrList) > 0 then
                            set acctEmail to item 1 of addrList
                        end if
                    end try
                    set output to output & "=== ACCOUNT: " & acctName & " <" & acctEmail & "> (" & acctCount & " messages) ===" & return
                    set output to output & acctOutput & return
                    set totalCount to totalCount + acctCount
                end if

            end try
        end if
    end repeat
end tell

if totalCount > 0 then
    return "TOTAL:" & totalCount & return & output
else
    return "NO_MAIL: No messages in the last 24 hours across all accounts."
end if
