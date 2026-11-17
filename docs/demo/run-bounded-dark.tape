Output docs/demo/run-bounded-dark.gif
Set Theme "Catppuccin Mocha"
Set Shell bash
Set FontSize 18
Set Width 1200
Set Height 520
Set WindowBar Colorful
Set TypingSpeed 45ms
Hide
Set TypingSpeed 0
Sleep 500ms
Type `export PS1='$ ' PATH="$PWD/target/release:$PATH"`
Enter
Sleep 500ms
Type "clear"
Enter
Sleep 600ms
Show
Set TypingSpeed 45ms
Type "run-bounded --timeout 5 --idle 2 --max-output 1K -- printf 'bounded output\n'"
Enter
Sleep 2s
