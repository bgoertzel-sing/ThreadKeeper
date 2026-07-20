;;; =======================================================================
;;; Mechanism 3: continuity ledger schema and validation (guide §4.5, §5.4).
;;;
;;; Append-only event chain. Every committed self-change records cycle,
;;; actor, proposal fingerprint, before/after structural signatures,
;;; capability delta, and result, linked by parent event id. Genesis has
;;; parent `genesis`. Startup lineage validation compares the CURRENT
;;; structural signature against the latest ledger after-signature and
;;; reports a break on any drift (guide §4.5.2).
;;;
;;;   (continuity-event $seq $eid $parent $cycle $cid $actor $proposal-fp
;;;                     $before-sig $after-sig $capability-delta $result)
;;; =======================================================================

(= (init-continuity!)
   (progn (bind! &continuity-counter (new-state 0))
          (bind! &continuity-last (new-state genesis))
          continuity-ready))

(= (append-continuity-event! $cid $k $actor $proposal-fp $before-sig $after-sig $cap-delta $result)
   (let* (($n (+ 1 (get-state &continuity-counter)))
          ($eid (cev $n))
          ($parent (get-state &continuity-last)))
     (progn (change-state! &continuity-counter $n)
            (add-atom &self
              (continuity-event $n $eid $parent $k $cid $actor $proposal-fp
                                $before-sig $after-sig $cap-delta $result))
            (change-state! &continuity-last $eid)
            $eid)))

(= (continuity-event-of-seq $n)
   (collapse (match &self
               (continuity-event $n $eid $p $k $cid $a $fp $b $af $d $r)
               (continuity-event $n $eid $p $k $cid $a $fp $b $af $d $r))))

(= (event-eid-of    (continuity-event $n $eid $p $k $cid $a $fp $b $af $d $r)) $eid)
(= (event-parent-of (continuity-event $n $eid $p $k $cid $a $fp $b $af $d $r)) $p)
(= (event-after-of  (continuity-event $n $eid $p $k $cid $a $fp $b $af $d $r)) $af)

;;; Chain validation: event 1 must have parent genesis; event n must have
;;; parent (cev n-1). Any break fails closed with (chain-broken $n).
(= (validate-chain-step $n)
   (let $found (continuity-event-of-seq $n)
     (if (== $found ())
       (chain-missing $n)
       (let ($h $t) (decons $found)
         (if (== $n 1)
           (if (== (event-parent-of $h) genesis) chain-ok (chain-broken $n))
           (if (== (event-parent-of $h) (cev (- $n 1))) chain-ok (chain-broken $n)))))))

(= (validate-continuity-chain $n)
   (if (== $n 0)
     chain-ok
     (let $rest (validate-continuity-chain (- $n 1))
       (if (== $rest chain-ok)
         (validate-chain-step $n)
         $rest))))

(= (validate-full-chain)
   (validate-continuity-chain (get-state &continuity-counter)))

;;; Startup lineage check: the current structural signature must equal the
;;; latest ledger after-signature. With no events, only the exact genesis
;;; signature is acceptable (an empty protected surface); anything else is
;;; an untracked-state break.
(= (latest-after-signature)
   (let $n (get-state &continuity-counter)
     (if (== $n 0)
       no-events
       (let $found (continuity-event-of-seq $n)
         (if (== $found ())
           no-events
           (let ($h $t) (decons $found) (event-after-of $h)))))))

(= (lineage-check $current-sig)
   (let $latest (latest-after-signature)
     (if (== $latest no-events)
       lineage-break-untracked-state
       (if (== $latest $current-sig)
         lineage-ok
         (lineage-break $latest $current-sig)))))
