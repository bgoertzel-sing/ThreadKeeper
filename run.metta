!(import! &self (library lib_import))
!(git-import! "https://github.com/asi-alliance/OmegaClaw-Core.git")
!(import! &self (library OmegaClaw-Core lib_omegaclaw))

!(let $temp (cut) (py-call (telegram.start_telegram "-5437945421" 3)))
!(omegaclaw)
