from flask import Blueprint, current_app, jsonify, request

from .input import request_material
from .modules.archive.service import archive
from .modules.archive.storage import get, recent
from .modules.certificate.service import certify
from .modules.reswitch import service as reswitch
from .pipeline import run

api = Blueprint('fiber_api', __name__, url_prefix='/api')


def calculate(stage):
    return run(request_material(), stage)[stage]


@api.post('/trace')
def trace():
    return calculate('trace')


@api.post('/calibration')
def calibration():
    return calculate('calibration')


@api.post('/pairs')
def pairs():
    return calculate('pairs')


@api.post('/loss')
def loss():
    return calculate('loss')


@api.post('/design')
def design():
    return calculate('design')


@api.post('/inspections')
def submit():
    report, fresh = archive(current_app.config['DATABASE'], request_material())
    return {'inspection_id':report['inspection_id'], 'created':fresh, 'record':report}, 201 if fresh else 200


@api.get('/inspections')
def list_inspections():
    return {'inspections':recent(current_app.config['DATABASE'])}


@api.get('/inspections/<identifier>')
def inspection(identifier):
    return get(current_app.config['DATABASE'], identifier)


@api.get('/certificate/<identifier>')
def certificate(identifier):
    return certify(inspection(identifier))


@api.get('/export/<identifier>')
def export(identifier):
    response = jsonify(certificate(identifier))
    response.headers['Content-Disposition'] = f'attachment; filename="splice-{identifier[:12]}.json"'
    return response


@api.post('/reswitch/plan')
def reswitch_plan():
    return reswitch.schedule(request.get_json(silent=True))


@api.post('/reswitch/jobs')
def reswitch_open():
    body, status = reswitch.open_job(current_app.config['DATABASE'], request.get_json(silent=True))
    return body, status


@api.post('/reswitch/jobs/<archive_id>/resume')
def reswitch_resume(archive_id):
    body = request.get_json(silent=True) or {}
    return reswitch.resume(current_app.config['DATABASE'], archive_id,
                           body.get('occupancy') or {'services': []})


@api.get('/reswitch/jobs/<archive_id>/signoff')
def reswitch_signoff(archive_id):
    return reswitch.signoff(current_app.config['DATABASE'], archive_id)
