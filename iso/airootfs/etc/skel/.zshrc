# Arches live environment — baseline zsh config

# History
HISTFILE=~/.zsh_history
HISTSIZE=1000
SAVEHIST=1000
setopt append_history share_history hist_ignore_dups hist_ignore_space

# Navigation & editing
setopt auto_cd interactive_comments

# Completion
autoload -Uz compinit && compinit
zstyle ':completion:*' menu select
zstyle ':completion:*' matcher-list 'm:{a-z}={A-Z}'

# Prompt
autoload -Uz promptinit && promptinit
PS1='%F{cyan}%n%f@%F{green}%m%f:%F{blue}%~%f%# '
