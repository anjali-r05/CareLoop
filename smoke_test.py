"""Isolated route smoke test. Run after installing requirements: python smoke_test.py"""
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
import app as careloop

with tempfile.TemporaryDirectory(prefix='careloop-smoke-') as tmp:
    root = Path(tmp)
    careloop.AUTH_DB = root / 'accounts.db'
    careloop.DB = root / 'unused.db'
    careloop.USERS_DIR = root / 'workspaces'
    careloop.USERS_DIR.mkdir()
    careloop.init_db()
    careloop.app.config.update(TESTING=True, SECRET_KEY='smoke-test-secret')
    client = careloop.app.test_client()

    landing = client.get('/')
    assert landing.status_code == 200 and b'Keep everyone in sync' in landing.data, 'Landing page did not render'
    assert client.get('/login').status_code == 200, 'Sign-in page did not render'
    assert client.get('/register').status_code == 200, 'Registration page did not render'
    assert client.get('/dashboard').status_code in (301, 302), 'Dashboard should require sign-in'

    created = client.post('/register', data={
        'name': 'Smoke Test', 'email': 'smoke@example.com',
        'password': 'correct-horse-2026', 'confirm': 'correct-horse-2026'
    }, follow_redirects=True)
    assert created.status_code == 200 and b'Good care happens' in created.data, 'Registration-to-dashboard flow failed'
    assert b'Log out' in created.data and b'Public landing page' not in created.data, 'Account navigation/logout control missing'
    signed_in_landing = client.get('/')
    assert signed_in_landing.status_code == 200, 'Public landing page should remain accessible while signed in'
    assert client.get('/login').status_code in (301, 302), 'Signed-in users should not be asked to sign in again'

    due = (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M')
    added = client.post('/tasks/add', data={
        'title': 'Confirm transport for clinic visit', 'due_at': due,
        'owner': 'Alex', 'category': 'Appointment', 'priority': 'High'
    }, follow_redirects=True)
    assert added.status_code == 200 and b'Confirm transport for clinic visit' in added.data, 'Task creation failed'
    assert client.get('/tasks?q=transport').status_code == 200, 'Task search failed'
    task_id = careloop.connect().execute('SELECT id FROM tasks ORDER BY id DESC LIMIT 1').fetchone()['id']
    assert client.post(f'/tasks/{task_id}/update', data={'title':'Confirm transport for clinic visit','due_at':due,'owner':'Alex','category':'Appointment','priority':'High'}, follow_redirects=True).status_code == 200, 'Task edit failed'
    assert client.post(f'/tasks/{task_id}/snooze', follow_redirects=True).status_code == 200, 'Task reschedule failed'
    assert client.post(f'/tasks/{task_id}/block', data={'blocked_reason':'Waiting for the transport contact to confirm.'}, follow_redirects=True).status_code == 200, 'Task block failed'
    assert client.get(f'/tasks/{task_id}/activity').status_code == 200, 'Task activity endpoint failed'
    activity = client.get(f'/tasks/{task_id}/activity').get_json()
    assert any(e['event_type']=='Blocked' for e in activity['events']), 'Blocker event was not recorded'
    assert client.get('/tasks?status=Blocked').status_code == 200, 'Blocked filter failed'
    assert client.post(f'/tasks/{task_id}/block', follow_redirects=True).status_code == 200, 'Task unblock failed'
    assert client.post(f'/tasks/{task_id}/ack', follow_redirects=True).status_code == 200, 'Task acknowledgement failed'
    # Advanced execution workflow: checklist + prerequisite + traceable progress.
    assert client.post(f'/tasks/{task_id}/steps', data={'step_title':'Verify the appointment time'}, follow_redirects=True).status_code == 200, 'Checklist creation failed'
    step_id = careloop.connect().execute('SELECT id FROM action_steps WHERE task_id=?',(task_id,)).fetchone()['id']
    assert client.post(f'/steps/{step_id}/toggle', follow_redirects=True).status_code == 200, 'Checklist completion failed'
    assert client.post(f'/tasks/{task_id}/update-note', data={'update_type':'Handoff','update_body':'Contact details still need confirmation by the assigned owner.'}, follow_redirects=True).status_code == 200, 'Progress/handoff log failed'
    due2 = (datetime.now() + timedelta(days=3)).strftime('%Y-%m-%dT%H:%M')
    assert client.post('/tasks/add', data={'title':'Receive appointment confirmation','due_at':due2,'owner':'Alex','category':'Coordination','priority':'Normal'}, follow_redirects=True).status_code == 200, 'Prerequisite task creation failed'
    prerequisite_id = careloop.connect().execute('SELECT id FROM tasks ORDER BY id DESC LIMIT 1').fetchone()['id']
    assert client.post(f'/tasks/{task_id}/dependency', data={'depends_on_id':prerequisite_id}, follow_redirects=True).status_code == 200, 'Dependency link failed'
    # A dependent action must not close before its prerequisite is complete.
    assert client.post(f'/tasks/{task_id}/done', data={'completion_note':'Transport was confirmed.'}, follow_redirects=True).status_code == 200
    assert careloop.connect().execute('SELECT status FROM tasks WHERE id=?',(task_id,)).fetchone()['status']=='Open', 'Dependency gate did not prevent premature completion'
    assert client.post(f'/tasks/{prerequisite_id}/done', data={'completion_note':'Appointment time confirmed with the clinic.'}, follow_redirects=True).status_code == 200, 'Prerequisite completion failed'
    assert client.post(f'/tasks/{task_id}/done', data={'completion_note':'Transport was confirmed with the assigned contact.'}, follow_redirects=True).status_code == 200, 'Task completion failed'
    assert client.post(f'/tasks/{task_id}/done', follow_redirects=True).status_code == 200, 'Task reopen failed'
    # Recurring care routine: completing an occurrence creates exactly one next occurrence.
    recurring_due = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
    assert client.post('/tasks/add', data={'title':'Weekly check-in with care partner','due_at':recurring_due,'owner':'Alex','category':'Coordination','priority':'Normal','recurrence':'Weekly'}, follow_redirects=True).status_code == 200
    recurring_id = careloop.connect().execute('SELECT id FROM tasks WHERE title=? ORDER BY id DESC LIMIT 1',('Weekly check-in with care partner',)).fetchone()['id']
    assert client.post(f'/tasks/{recurring_id}/done', data={'completion_note':'Weekly coordination check-in completed.'}, follow_redirects=True).status_code == 200
    next_occurrence = careloop.connect().execute("SELECT * FROM tasks WHERE recurrence_parent_id=? AND status='Open'",(recurring_id,)).fetchone()
    assert next_occurrence is not None and next_occurrence['recurrence']=='Weekly', 'Next recurring occurrence was not generated'
    assert client.post(f'/tasks/{recurring_id}/done', follow_redirects=True).status_code == 200
    assert careloop.connect().execute("SELECT COUNT(*) FROM tasks WHERE recurrence_parent_id=? AND status='Open'",(recurring_id,)).fetchone()[0] == 1, 'Recurring generator created duplicate open occurrences'

    assert client.get('/tasks/export.csv').status_code == 200, 'CSV export failed'
    assert client.get('/sentinel').status_code == 200, 'Sentinel page failed'
    assert client.post('/sentinel', follow_redirects=True).status_code == 200, 'Sentinel review run failed'
    assert client.get('/gaps').status_code == 200, 'CareGap Radar page failed'
    assert client.get('/memory').status_code == 200, 'Care Memory page failed'
    assert client.get('/visit-brief').status_code == 200, 'Visit Brief page failed'
    # Distinct workflows: request intake, accountable conversion, operational readiness, accepted handoff.
    assert client.get('/intake').status_code == 200, 'Request intake page failed'
    assert client.post('/intake', data={'request_text':'Arrange transport for the scheduled follow-up visit.','need_type':'Transport','requested_by':'Family coordinator','preferred_by':'','constraints':'Pickup time must be confirmed.'}, follow_redirects=True).status_code == 200, 'Request intake capture failed'
    intake_id = careloop.connect().execute('SELECT id FROM intakes ORDER BY id DESC LIMIT 1').fetchone()['id']
    assert client.post(f'/intake/{intake_id}/convert', data={'title':'Confirm transport for follow-up visit','owner':'Alex','due_at':due,'priority':'High'}, follow_redirects=True).status_code == 200, 'Reviewed intake conversion failed'
    intake_row = careloop.connect().execute('SELECT status,converted_task_id FROM intakes WHERE id=?',(intake_id,)).fetchone()
    assert intake_row['status']=='Converted' and intake_row['converted_task_id'], 'Intake conversion link was not persisted'
    assert client.get('/readiness').status_code == 200, 'Plan readiness page failed'
    assert client.get('/handoffs').status_code == 200, 'Team handoff page failed'
    assert client.post('/handoffs', data={'title':'Evening-to-morning handoff','from_person':'Alex','to_person':'Jordan','summary':'Transport request captured; pickup time is awaiting confirmation.','watch_items':'Do not assume the pickup is booked until the provider confirms.','next_steps':'Jordan to confirm the pickup time and update the linked action.','task_ids':[str(intake_row['converted_task_id'])]}, follow_redirects=True).status_code == 200, 'Handoff creation failed'
    handoff_id = careloop.connect().execute('SELECT id FROM handoffs ORDER BY id DESC LIMIT 1').fetchone()['id']
    assert client.post(f'/handoffs/{handoff_id}/view', follow_redirects=True).status_code == 200, 'Handoff viewed transition failed'
    assert careloop.connect().execute('SELECT status,viewed_at FROM handoffs WHERE id=?',(handoff_id,)).fetchone()['status']=='Viewed', 'Handoff view was not recorded'
    assert client.post(f'/handoffs/{handoff_id}/ack', follow_redirects=True).status_code == 200, 'Handoff acknowledgement failed'
    assert careloop.connect().execute('SELECT status,acknowledged_at FROM handoffs WHERE id=?',(handoff_id,)).fetchone()['status']=='Acknowledged', 'Handoff acceptance was not recorded'
    assert client.post(f'/handoffs/{handoff_id}/start', follow_redirects=True).status_code == 200, 'Handoff start transition failed'
    assert client.post(f'/handoffs/{handoff_id}/complete', follow_redirects=True).status_code == 200, 'Handoff completion transition failed'
    final_handoff=careloop.connect().execute('SELECT status,completed_at FROM handoffs WHERE id=?',(handoff_id,)).fetchone()
    assert final_handoff['status']=='Completed' and final_handoff['completed_at'], 'Handoff completion was not persisted'
    assert careloop.connect().execute('SELECT COUNT(*) FROM handoff_events WHERE handoff_id=?',(handoff_id,)).fetchone()[0] >= 4, 'Handoff event history was not recorded'
    assert client.get('/journeys').status_code == 200, 'Care journeys page failed'
    journey = client.post('/journeys', data={
        'title':'Appointment coordination', 'journey_type':'Appointment preparation',
        'goal':'Ensure logistics and questions are ready before the visit.',
        'start_date':datetime.now().date().isoformat(),
        'target_date':(datetime.now()+timedelta(days=5)).date().isoformat()
    }, follow_redirects=True)
    assert journey.status_code == 200 and b'Appointment coordination' in journey.data, 'Journey creation failed'
    journey_id = careloop.connect().execute('SELECT id FROM journeys ORDER BY id DESC LIMIT 1').fetchone()['id']
    assert client.get('/voice').status_code == 200, 'Context-aware CarePilot page failed'
    # CarePilot parser should handle common Hinglish requests conservatively.
    parsed_hinglish = careloop.parse_request('Kal 10 baje appointment ke liye documents ready karna hai')
    assert parsed_hinglish['category'] == 'Appointment' and parsed_hinglish['due_at'], 'Future-action Hinglish kal should produce a reviewable date suggestion'
    parsed_today = careloop.parse_request('आज सुबह 10:00 बजे डॉक्टर की appointment है')
    assert parsed_today['category'] == 'Appointment' and parsed_today['due_at'].endswith('T10:00'), 'Hindi today + clock time extraction failed'
    parsed_tomorrow = careloop.parse_request('Tomorrow at 8:30 PM call the clinic')
    assert parsed_tomorrow['category'] == 'Appointment' or parsed_tomorrow['category'] == 'Follow-up'
    assert parsed_tomorrow['due_at'].endswith('T20:30'), 'Tomorrow time extraction failed'
    parsed_today = careloop.parse_request('आज सुबह 10:00 बजे डॉक्टर की appointment है')
    assert parsed_today['category'] == 'Appointment' and parsed_today['due_at'].endswith('T10:00'), 'Hindi today + clock time extraction failed'
    parsed_tomorrow = careloop.parse_request('Tomorrow at 8:30 PM call the clinic')
    assert parsed_tomorrow['category'] == 'Appointment' or parsed_tomorrow['category'] == 'Follow-up'
    assert parsed_tomorrow['due_at'].endswith('T20:30'), 'Tomorrow time extraction failed'
    parsed_status = careloop.parse_request('kya documents ready ho gaye hai?')
    assert parsed_status['title'].lower().startswith('check status:'), 'Status question should become a follow-up action'
    parsed_month_date = careloop.parse_request('Doctor appointment is on 12 November 2026 at 11:30 AM.')
    assert parsed_month_date['due_at'] == '2026-11-12T11:30', 'English month-name date and 12-hour time extraction failed'
    parsed_complex = careloop.parse_request("Mr. Sharma has a health checkup scheduled for 12 November 2026 at 11:30 AM. Arrange transportation, collect blood test reports, prepare his medication list and inform the family caregiver one day before the appointment.")
    assert len(parsed_complex['actions']) == 4, 'Complex care coordination request should produce four editable actions'
    assert parsed_complex['actions'][1]['due_at'] == '2026-11-11T10:00', 'One-day-before relative due date extraction failed'
    assert client.post('/voice/transcribe', data={}, follow_redirects=False).status_code == 400, 'Gnani transcription must validate audio/key configuration'
    assert b'WORKSPACE CONTEXT' in client.get('/voice').data, 'CarePilot workspace context panel missing'
    assert b'CarePilot' in client.get('/dashboard').data and b'06' in client.get('/dashboard').data, 'CarePilot should be positioned as the final workflow feature'
    # Care Context note can be associated with a task and a journey.
    assert client.post('/memory', data={'title':'Transport confirmation context','body':'Pickup contact has not confirmed the time yet.','tag':'Logistics','task_id':str(task_id),'journey_id':str(journey_id)}, follow_redirects=True).status_code == 200, 'Linked context note save failed'
    linked_note = careloop.connect().execute('SELECT task_id,journey_id FROM notes WHERE title=?',('Transport confirmation context',)).fetchone()
    assert linked_note and linked_note['task_id']==task_id and linked_note['journey_id']==journey_id, 'Note associations were not persisted'
    assert client.post(f'/journeys/{journey_id}/milestones', data={
        'title':'Confirm appointment time', 'due_at':due, 'owner':'Alex', 'priority':'High'
    }, follow_redirects=True).status_code == 200, 'Journey milestone creation failed'
    assert careloop.connect().execute('SELECT COUNT(*) FROM tasks WHERE journey_id=?',(journey_id,)).fetchone()[0] == 1, 'Journey milestone was not linked to care plan'
    assert client.post('/voice/save', data={'title':'Confirm transport follow-up','due_at':due,'owner':'Alex','category':'Coordination','priority':'Normal','source':'CarePilot · reviewed','original_request':'Please confirm transport for the appointment','journey_id':str(journey_id)}, follow_redirects=True).status_code == 200, 'CarePilot-to-plan conversion failed'
    pilot_task = careloop.connect().execute("SELECT id,journey_id FROM tasks WHERE title='Confirm transport follow-up' ORDER BY id DESC LIMIT 1").fetchone()
    assert pilot_task and pilot_task['journey_id']==journey_id, 'CarePilot journey link was not persisted'
    assert careloop.connect().execute("SELECT COUNT(*) FROM task_updates WHERE task_id=? AND update_type='CarePilot source'",(pilot_task['id'],)).fetchone()[0] == 1, 'Original CarePilot request was not retained in task history'
    status_payload = client.get('/api/status').get_json()
    assert status_payload['gnani_connected'] is False and status_payload['gnani_integration_tested'] is False, 'Gnani status must not claim an untested integration'
    assert client.get(f'/journeys/{journey_id}').status_code == 200, 'Journey detail page failed'
    assert client.post(f'/journeys/{journey_id}/status', data={'action':'pause'}, follow_redirects=True).status_code == 200, 'Journey pause failed'
    assert careloop.connect().execute('SELECT status FROM journeys WHERE id=?',(journey_id,)).fetchone()['status']=='Paused', 'Journey pause state was not saved'
    assert client.post(f'/journeys/{journey_id}/status', data={'action':'resume'}, follow_redirects=True).status_code == 200, 'Journey resume failed'

    signed_out = client.post('/logout', follow_redirects=True)
    assert signed_out.status_code == 200 and b'Sign in to CareLoop' in signed_out.data, 'Logout flow failed'
    public_landing_after_logout = client.get('/')
    assert public_landing_after_logout.status_code == 200 and b'Create account' in public_landing_after_logout.data and b'Sign In' in public_landing_after_logout.data, 'Landing page should show Sign In and Create account after logout'
    assert b'Open dashboard' not in public_landing_after_logout.data, 'Landing page must not show a dashboard option'
    login = client.post('/login', data={'email':'smoke@example.com','password':'correct-horse-2026'}, follow_redirects=True)
    assert login.status_code == 200 and b'Good care happens' in login.data, 'Sign-in flow failed'

print('CareLoop route smoke test passed: auth, intake-to-action conversion, task execution, journeys, readiness, handoffs, Sentinel, Radar, memory and visit brief.')
