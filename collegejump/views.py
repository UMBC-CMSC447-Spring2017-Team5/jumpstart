import datetime
import os
import random
import traceback
import flask
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound
from werkzeug.exceptions import HTTPException, InternalServerError
from collegejump import app, forms, models, database, admin_required

@app.route('/static/<path:path>')
def send_static(path):
    return flask.send_from_directory('static', path)


@app.route('/', methods=["GET", "POST"])
def front_page():
    # If setup mode is happening, render a FirstSetupUserInfoForm.
    if 'SETUP_KEY' in app.config:
        setup_form = forms.FirstSetupUserInfoForm()
        # Remove some fields from this form, because it's a new user, which is
        # automatically an admin.
        del setup_form.admin
        del setup_form.mentors
        del setup_form.semesters_enrolled
        del setup_form.delete
    else:
        setup_form = None


    if setup_form and setup_form.validate_on_submit():
        user = setup_form.to_user_model()
        user.admin = True # A user created this way is always an admin

        app.db.session.add(user)
        app.db.session.commit()

        # Now that the user is created, log it and delete the SETUP_KEY.
        app.logger.info("Created user %r using SETUP_KEY, disabling SETUP_KEY", user)
        del app.config['SETUP_KEY']

        # Log the created user in automatically.
        login_user(user)

        return flask.redirect(flask.url_for("front_page"))

    announcements = models.Announcement.query\
            .order_by(models.Announcement.timestamp.desc()).limit(10)
    return flask.render_template('index.html',
                                 announcements=announcements,
                                 setup_form=setup_form)


@app.route('/calendar')
def calendar_page():
    return flask.render_template('calendar.html')


@app.route('/login', methods=['GET', 'POST'])
def login_page():
    # Get the `returnto` or `next` query string, otherwise leave blank.
    returnto = flask.request.args.get('returnto') \
            or flask.request.args.get('next') \
            or ''

    # Create the form object with its defaults. If a `returnto` was submitted,
    # it will be preserved here.
    form = forms.LoginForm(returnto=returnto)

    # If the user is already logged in, skip the login page and return them to
    # wherever returnto is.
    if current_user.is_authenticated:
        return form.redirect()

    # If the form was POSTed, validate it. If it passes validation, then the
    # User's credentials are correct, and they may be logged in.
    if form.validate_on_submit():
        # During validation, the user object is stored as `user_model`.
        success = login_user(form.user_model)
        if success:
            app.logger.info("Successful login by %r", form.user_model)
            flask.flash('Login successful.', 'success')
            return form.redirect()
        else:
            # If the login failed here, it's not because of the
            # password. Maybe the user is inactive?
            app.logger.warning("Unexpected login failure by %r", form.user_model)
            flask.flash('Unexpected login failure', 'error')

    return flask.render_template('login.html', form=form)

@app.route('/logout', methods=['GET', 'POST'])
@login_required
def logout_page():
    logout_user()
    return flask.redirect(flask.url_for('front_page'))

@app.route('/account/<int:user_id>', methods=['GET', 'POST'])
@login_required
def account_settings_page(user_id):
    # Unless the user is editing their own account, or is an admin, deny
    # access.
    if (not current_user.admin) and (current_user.id != user_id):
        return flask.abort(403)

    # Load the user. If we have reached this point, the user is authorized to
    # edit this account, so if this user ID doesn't exist, throw a NotFound
    user = models.User.query.get(user_id)
    if user is None:
        return flask.abort(404)

    # Construct a pre-filled form.
    # Populate semester data in the form. This is separate from the initializer
    # because it requires a database query.
    form = forms.UserForm(name=user.name,
                          email=user.email.lower(),
                          admin=user.admin,
                          mentors=','.join([m.email for m in user.mentors]),
                          semesters_enrolled=[s.id for s in user.semesters],
                          require_password=False) # can change password, don't have to
    form.populate_semesters()

    # If the user is not an admin, serve them a limited form. If the user
    # submits a form with these filled anyway, they will be deleted at this
    # stage.
    if not current_user.admin:
        del form.email
        del form.admin
        del form.mentors
        del form.semesters_enrolled
        del form.delete

    # We check if the user is to be deleted. For this, we don't yet validate the
    # form, because not all of the fields need to be valid.
    if flask.request.method == 'POST' and form.delete and form.delete.data:
        # Retrieve the user for logging, then delete it.
        app.db.session.delete(user)
        app.db.session.commit()
        app.logger.info("Deleted user %r from the database", user)
        flask.flash("Deleted user '{}'".format(user.email.lower()), 'success')
        return flask.redirect(flask.url_for('front_page'))

    # Otherwise, we update the user information.
    if form.validate_on_submit():
        # Fill out the data using the existing model.
        form.to_user_model(user=user)

        app.db.session.commit()
        app.logger.info("Updated user %r", user)
        flask.flash("Updated user '{}'".format(user.email.lower()))
        return flask.redirect(flask.url_for('account_settings_page',
                                            user_id=user_id))

    return flask.render_template('account_settings.html', user=user, form=form)

@app.route('/announcement/new', methods=['GET', 'POST'])
@app.route('/announcement/<int:announcement_id>/edit', methods=['GET', 'POST'])
@admin_required
def edit_announcement_page(announcement_id=None):
    form = forms.AnnouncementForm()

    if announcement_id is not None:
        new_announcement = False
        announcement = models.Announcement.query.get(announcement_id)
        if announcement is None:
            return flask.abort(404)
    else:
        new_announcement = True
        announcement = models.Announcement(current_user.email.lower(), '', '')
        # Remove the delete button if we're editing a new announcement.
        del form.delete

    # If we're deleting the announcement, we don't even need to validate the
    # whole form, just check for the delete button. We do have to be careful
    # that it hasn't been deleted, though.
    if flask.request.method == 'POST' and form.delete and form.delete.data:
        # At this point, the announcement already exists and is in the
        # database, so we can safely delete it.
        app.db.session.delete(announcement)
        app.db.session.commit()
        app.logger.info("Deleted announcement %r from the database", announcement)
        flask.flash("Deleted announcement '{}'".format(announcement.title), 'success')
        return flask.redirect(flask.url_for("front_page"))

    # If the form was submitted and valid, change the object and redirect to
    # viewing it.
    if form.validate_on_submit() and form.submit.data:
        # Update the announcement from the form if we're submitting finally.
        announcement.title = form.title.data
        announcement.content = form.content.data
        # If the announcement is brand new, add it to the session.
        if new_announcement:
            flask.flash('Announcement posted successfully!', 'success')
            app.db.session.add(announcement)
        else:
            flask.flash('Announcement updated successfully!', 'success')
        app.db.session.commit()
        return flask.redirect(flask.url_for('front_page'))

    # Otherwise, if we're doing the GET method, fill the form with the original
    # data.
    elif flask.request.method == 'GET':
        form.title.data = announcement.title
        form.content.data = announcement.content

    return flask.render_template('edit_announcement.html',
                                 announcement_id=announcement.id,
                                 author=announcement.author,
                                 form=form)

@app.route('/syllabus/', methods=["GET", "POST"])
@admin_required
def syllabus_page():
    form = forms.SemesterForm()
    if form.validate_on_submit():
        semester = models.Semester(form.name.data, form.order.data)
        app.db.session.add(semester)
        app.db.session.commit()

        return flask.redirect(flask.url_for('syllabus_page'))

    all_semesters = models.Semester.query.order_by('order')
    return flask.render_template("syllabus_root.html", all_semesters=all_semesters, form=form)

@app.route('/syllabus/semester/<int:semester_id>', methods=["GET", "POST"])
@admin_required
def edit_semester_page(semester_id):
    """Edit the syllabus for a whole semester."""
    # pylint: disable=no-member
    # Look the semester up by id, eagerly loading the weeks, so they can be
    # displayed or changed without firing a bunch of extra queries.
    semester = models.Semester.query\
            .options(joinedload(models.Semester.weeks)) \
            .get(semester_id)
    if semester is None:
        return flask.abort(404)

    # Construct a form for editing the current semester.
    semester_form = forms.SemesterForm(name=semester.name,
                                       order=semester.order)

    # Construct a form for making a new week in this semester. Because it is
    # new, it doesn't need the delete button or file upload field.
    week_form = forms.WeekForm()
    del week_form.delete
    del week_form.new_document
    del week_form.assignment_name
    del week_form.assignment_instructions

    # If the delete button is pressed on the semester, delete it without
    # validating the rest of the input.
    if flask.request.method == 'POST' and semester_form.delete.data:
        app.logger.info("Deleting %r", semester)
        app.db.session.delete(semester)
        app.db.session.commit()
        flask.flash("Deleted semester '{}'".format(semester.name), 'success')
        return flask.redirect(flask.url_for('syllabus_page'))

    # If POSTing a valid semester form, apply the changes.
    if semester_form.validate_on_submit():
        semester.name = semester_form.name.data
        semester.order = semester_form.order.data

        # Commit at the end of processing the database.
        app.db.session.commit()

        # Redirect, so the page is loaded again by GET with changes made.
        flask.flash("Updated semester '{}'".format(semester.name), 'success')
        return flask.redirect(flask.url_for('edit_semester_page', semester_id=semester.id))

    # If POSTing a valid new week, create it and attach it to this semester as
    # the last week.
    if week_form.validate_on_submit():
        week = models.Week(semester_id, len(semester.weeks) + 1,
                           week_form.header.data,
                           week_form.intro.data)
        app.logger.debug("Creating new week %r", week)
        app.db.session.add(week)
        app.db.session.commit()
        app.logger.info("Creating %r", week)
        flask.flash("Created week {} in semester '{}'".format(week.week_num, semester.name),
                    'success')
        return flask.redirect(flask.url_for('edit_semester_page', semester_id=semester.id))

    return flask.render_template("semester.html",
                                 semester=semester,
                                 semester_form=semester_form,
                                 week_form=week_form)

@app.route('/syllabus/semester/<int:semester_id>/week/<int:week_num>', methods=["GET", "POST"])
@admin_required
def edit_week_page(semester_id, week_num):
    """Edit a particular week in a semester."""

    # We aren't given the week ID, just the semester ID and week number, so we
    # look it up by those. The database guarantees that the pair is unique.
    try:
        week = models.Week.query.filter_by(semester_id=semester_id,
                                           week_num=week_num).one()
    except NoResultFound:
        return flask.abort(404)
    # Retrieve the single assignment we support. Some day, we should support
    # multiple.
    assignment = week.assignments[0] if week.assignments else models.Assignment()

    form = forms.WeekForm(header=week.header,
                          intro=week.intro,
                          assignment_name=assignment.name,
                          assignment_instructions=assignment.instructions)

    # If the delete button was pressed, delete the week.
    if flask.request.method == 'POST' and form.delete.data:
        app.db.session.delete(week)

        # Iterate through all of the other weeks in the semester, and change
        # their week numbers to match.
        for other_week in week.semester.weeks:
            # If this week is after what we're deleting, shift it back.
            if other_week.week_num > week.week_num:
                other_week.week_num -= 1

        app.db.session.commit()
        app.logger.info("Deleted week %r from the database", week)
        flask.flash("Deleted week '{}'".format(week.header), 'success')
        return flask.redirect(flask.url_for("edit_semester_page",
                                            semester_id=week.semester_id))

    # Otherwise, if the form was submitted, validate it and update the week data from it.
    elif form.validate_on_submit() and form.submit.data is True:
        app.logger.debug("Updating week %r from form", week)
        week.header = form.header.data
        week.intro = form.intro.data

        # Construct an assignment, or re-fill an existing one. Right now, this
        # is just a single element list, though in the future it should be made
        # for multiple.
        assignment.name = form.assignment_name.data \
                if form.assignment_name else None
        assignment.instructions = form.assignment_instructions.data \
                if form.assignment_instructions else None

        # If the assignment has content, ensure it is attached to the week.
        # Otherwise, leave the week blank.
        if assignment.name or assignment.instructions:
            # It has content; make sure it's in the session.
            if assignment not in app.db.session:
                app.db.session.add(assignment)
            week.assignments = [assignment] # we only support 1

        else:
            week.assignments = []

        # If a file was uploaded, extract it into a Document, store that, and
        # associate it with this week.
        if form.new_document.has_file():
            app.logger.debug("Adding document to week %r", week)
            # Create the Document object using the form data.
            document = models.Document(os.path.basename(form.new_document.data.filename),
                                       form.new_document.data.stream.read())

            # Add the Document to the current session.
            app.db.session.add(document)

            # Associate the document with the week. The ORM will handle the
            # rest.
            week.documents.append(document)

        # Flush the changes to the session.
        app.db.session.commit()

        # If we submitted everything correctly, redirect to the view page.
        flask.flash("Updated week '{}'".format(week.header), 'success')
        return flask.redirect(flask.url_for('week_page',
                                            semester_id=semester_id,
                                            week_num=week_num))

    return flask.render_template('edit_week.html',
                                 week=week,
                                 form=form,
                                 show_edit_options=True)

@app.route('/announcement/')
@app.route('/announcement/<int:announcement_id>')
def announcement_page(announcement_id=None):
    if announcement_id is not None:
        announcement = models.Announcement.query.get(announcement_id)
        if announcement is None:
            return flask.abort(404)
        return flask.render_template('announcement.html', announcement=announcement)

    # Otherwise
    announcements = models.Announcement.query\
            .order_by(models.Announcement.timestamp.desc())
    return flask.render_template('all_announcements.html', announcements=announcements)


@app.route('/account/all', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_accounts_page():
    # Generate a form for a new user. Since it is a new user, it does not need the delete field.
    form = forms.UserForm()
    del form.delete
    form.populate_semesters()

    # The form validates all input, including uniqueness of the email, and
    # other database properties. If we encounter a database error at this
    # point, it is an actual error.
    if form.validate_on_submit():
        user = form.to_user_model()
        app.db.session.add(user)
        app.db.session.commit()

        app.logger.info("Created user %r in the database", user)
        return flask.redirect(flask.url_for('edit_accounts_page'))

    return flask.render_template('edit_accounts.html', form=form,
                                 users=models.User.query.all())

@app.route('/document/<int:document_id>')
@login_required
def document_page(document_id):
    document = models.Document.query.get(document_id)
    if document is None:
        return flask.abort(404)
    return flask.send_file(document.file_like(),
                           attachment_filename=document.name,
                           as_attachment=True)

@app.route('/document/<int:document_id>/remove', methods=["POST"])
@admin_required
def remove_document(document_id):
    # Get the `returnto` or `next` query string, otherwise leave blank.
    returnto = flask.request.args.get('returnto') \
            or flask.request.args.get('next') \
            or flask.url_for('front_page')

    redirectform = forms.RedirectForm(returnto=returnto)

    # Delete the document. We don't know if it exists when this happens.
    models.Document.query.filter_by(id=document_id).delete()
    app.db.session.commit()

    # Redirect back to whereever we came from, ensuring the URL is safe.
    return redirectform.redirect()

@app.route('/database/', methods=['GET', 'POST'])
@admin_required
def database_page():
    form = forms.DatabaseUploadForm()
    if form.validate_on_submit():
        # The data from the file is in bytes-like in form.zipfile.data, which we
        # pass to the import function.
        app.logger.info("Importing database from uploaded file by %r", current_user)
        database.import_db(form.zipfile.data)
        app.logger.info("Database import complete.")
        return flask.redirect(flask.url_for("front_page"))

    return flask.render_template('database.html', form=form)

@app.route('/database/export')
@admin_required
def database_export_endpoint():
    filename = datetime.datetime.now().strftime("collegejump-export-%Y%m%d.zip")
    return flask.send_file(database.export_db(),
                           attachment_filename=filename,
                           as_attachment=True,
                           mimetype='application/zip')


@app.route('/semester/<int:semester_id>/week/<int:week_num>', methods=["GET", "POST"])
@login_required
def week_page(semester_id, week_num):
    # We aren't given the week ID, just the semester ID and week number, so we
    # look it up by those. The database guarantees that the pair is unique.
    try:
        week = models.Week.query.filter_by(semester_id=semester_id,
                                           week_num=week_num).one()
    except NoResultFound:
        return flask.abort(404)

    # Select the first assignment if any.
    assignment = week.assignments[0] if week.assignments else None

    # The user must be 'interested' in this semester to view it: either they are
    # an admin, have a mentee enrolled in the semester, or are enrolled
    # themselves.
    if week.semester_id not in [s.id for s in current_user.interested_semesters()]:
        return flask.abort(403)

    # If there is an assignment, prepare a form to receive submissions.
    answer_form = forms.AnswerForm() if assignment else None

    if answer_form and answer_form.validate_on_submit():
        # This is an answer submission, so create a Submission.
        submission = answer_form.to_submission_model(assignment, current_user)
        app.db.session.commit()
        return flask.redirect(flask.url_for("week_page",
                                            semester_id=semester_id,
                                            week_num=week_num))

    submissions = models.Submission.query \
            .filter_by(author=current_user, assignment=assignment) \
            .order_by('timestamp desc')

    submissions_to_grade = models.Submission.query \
            .filter(models.Submission.author_id.in_([m.id for m in current_user.mentees]),
                    models.Submission.assignment == assignment) \
            .order_by('timestamp desc')

    return flask.render_template('week.html',
                                 week=week,
                                 submissions=submissions,
                                 submissions_to_grade=submissions_to_grade,
                                 answer_form=answer_form)

@app.route('/submission/<int:submission_id>/attachment')
@login_required
def submission_attachment_page(submission_id):
    submission = models.Submission.query.get(submission_id)
    if submission is None:
        return flask.abort(404)

    # Only let the author, the admins, and the author's mentors download the file.
    if (not current_user == submission.author) \
            and (not current_user.admin) \
            and (current_user not in submission.author.mentors):
        return flask.abort(403)

    return flask.send_file(submission.attachment_file_like(),
                           attachment_filename=submission.filename,
                           as_attachment=True)

@app.route('/submission/<submission_id>', methods=["GET", "POST"])
@login_required
def feedback_page(submission_id):
    # Get the `returnto` or `next` query string, otherwise leave blank.
    returnto = flask.request.args.get('returnto') \
            or flask.request.args.get('next') \
            or ''

    # Look up the response.
    submission = models.Submission.query.get(submission_id)
    if submission is None:
        return flask.abort(404)

    if (not current_user.admin) and (current_user not in submission.author.mentors):
        return flask.abort(403)

    feedback_form = forms.FeedbackForm(returnto=returnto)

    if feedback_form.validate_on_submit():
        feedback = models.Feedback()
        feedback.submission = submission
        feedback.text = feedback_form.feedback.data
        feedback.timestamp = datetime.datetime.now()
        feedback.author = current_user

        app.db.session.add(feedback)
        app.db.session.commit()
        return feedback_form.redirect()

    return flask.render_template("feedback.html",
                                 submission=submission,
                                 feedback_form=feedback_form)

@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(SQLAlchemyError)
def http_error_page(error):
    # If we are catching some non-http exception it is an application error.
    if not isinstance(error, HTTPException):
        # Replace the specific error with this generic one.
        original = error

        # Attach additional information for reporting.
        error = InternalServerError()
        error.incident_number = random.randint(2**8, 2**16)
        error.original = original
        error.traceback = '\n'.join(traceback.format_exception(None, original, original.__traceback__))

        # Log it, so if the recipient isn't an admin, the admins can later find
        # this in the logs.
        app.logger.error("Incident %d; please submit the following code to\n%s\n%s",
                         error.incident_number, app.config['REPOSITORY_ISSUES'], error.traceback)

    return flask.render_template('error.html', error=error), error.code
