-- Template for the Music Scripts menu items. install-scripts fills in ACTION,
-- PROJECT and PYTHON and compiles one copy per action.
-- The script only reads the selection; the organizer runs as its own process
-- so Music never waits on itself.
set wantsSelection to {{SELECTION}}
set ids to ""
if wantsSelection then
	tell application "Music"
		set sel to selection
		repeat with t in sel
			set ids to ids & " " & (persistent ID of t)
		end repeat
	end tell
	if ids is "" then
		display dialog "Select some songs first." with title "Music Organizer" buttons {"OK"} default button 1
		return
	end if
end if
do shell script "cd " & quoted form of "{{PROJECT}}" & " && nohup " & quoted form of "{{PYTHON}}" & ¬
	" organizer.py ui {{ACTION}}" & ids & " >> data/ui.log 2>&1 &"
