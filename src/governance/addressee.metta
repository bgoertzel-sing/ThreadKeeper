;; Revision 2 mention-first addressee policy. Python authenticates and
;; normalizes Telegram metadata; these relations keep priority inspectable.
(= (addressee-classification True True $reply-is-bot $has-addressing) DIRECT)
(= (addressee-classification True False $reply-is-bot $has-addressing) DIRECT)
(= (addressee-classification False True $reply-is-bot $has-addressing) DIRECT)
(= (addressee-classification False False True $has-addressing) SECONDARY)
(= (addressee-classification False False False False) GROUP)
(= (addressee-classification False False False True) INCIDENTAL)

(= (addressee-reinforced True True) True)
(= (addressee-reinforced True False) False)
(= (addressee-reinforced False True) False)
(= (addressee-reinforced False False) False)
