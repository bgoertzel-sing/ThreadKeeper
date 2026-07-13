;configured at runtime:
(= (IRC_channel) (empty))
(= (IRC_server) (empty))
(= (IRC_port) (empty))
(= (IRC_user) (empty))
(= (commchannel) (empty))
(= (MM_URL) (empty))
(= (MM_CHANNEL_ID) (empty))
(= (TG_CHAT_ID) (empty))
(= (TG_POLL_TIMEOUT) (empty))
(= (SL_CHANNEL_ID) (empty))
(= (SL_POLL_INTERVAL) (empty))

;Connect all the channels:
(= (initChannels) (let $temp (cut) True))

;Receive the latest user message considering all communication channels:
(= (receive)
   (py-call (telegram.getLastMessage)))

;Check whether the current message should be skipped (addressed to another bot):
(= (shouldSkipResponse)
   (py-call (telegram.should_skip_response)))

;Send a message to all communication channels.
;The Python transport owns bounded, per-chat deduplication and send logging.
;Do not deduplicate here: MeTTa's nondeterministic evaluation can update state
;on one branch without executing the transport call, silently losing a reply.
(= (send $msg)
   (let $safemsg (string-replace $msg  "\n" "\\n")
        (let $temp (cut) (py-call (telegram.send_message $safemsg)))))

;Search the internet for some information:
(= (search $msg)
   (py-call (websearch.search $msg)))
