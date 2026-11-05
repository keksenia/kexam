"""One localhost command to inspect or explicitly continue a saved check."""
import argparse
import http.client
import json
from pathlib import Path
import re

VERSION = 'continue-check-cli-1.0.0'
ROOT = Path(__file__).resolve().parents[1]


def request(port, method, path, body=None):
    connection=http.client.HTTPConnection('127.0.0.1',port,timeout=30)
    try:
        raw=json.dumps(body,ensure_ascii=False).encode() if body is not None else None
        connection.request(method,path,body=raw,headers={'Content-Type':'application/json','Origin':f'http://127.0.0.1:{port}'})
        response=connection.getresponse();value=json.loads(response.read())
        if response.status>=400: raise ValueError(value.get('error','Запрос не принят.'))
        return value
    finally: connection.close()


def main():
    parser=argparse.ArgumentParser(description='Состояние и адресное продолжение одной проверки. Без --submit запросов к моделям нет.')
    parser.add_argument('work',help='Номер S009, идентификатор работы E… или проверки C…')
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--save-form',type=Path,help='Сохранить шаблон короткого уточнения в новый файл')
    parser.add_argument('--submit',type=Path,help='Отправить заполненный шаблон')
    parser.add_argument('--confirm-paid',action='store_true',help='Явно разрешить новые запросы при отправке')
    args=parser.parse_args()
    if not 1<=args.port<=65535 or args.submit and args.save_form:
        parser.error('--save-form и --submit используются отдельно; нужен допустимый порт.')
    work=args.work.upper();label=work
    if re.fullmatch('S[0-9]{3}',work):
        cases=json.loads((ROOT/'data_local/m17/stobal-82-2026-10-26/index.json').read_text())['cases']
        case=next((c for c in cases if c['case']==work),None)
        if case is None: parser.error('Работа не найдена в пачке.')
        work=case['work_id']
    if re.fullmatch('E[0-9A-Fa-f]{32}',work):
        job=request(args.port,'GET','/api/works/'+work[0]+work[1:].lower()+'/check')
        if not job: parser.error('У работы пока нет попытки.')
        work=job['id']
    elif re.fullmatch('C[0-9A-Fa-f]{32}',work):work=work[0]+work[1:].lower()
    else:parser.error('Нужен номер S009 или идентификатор E…/C….')
    print(f'Работа: {label}. Проверка: {work}.')
    if args.submit:
        form=json.loads(args.submit.read_text());form['confirm_paid']=args.confirm_paid
        result=request(args.port,'POST','/api/checks/'+work+'/continue',form)
        print('Продолжение: '+result['job']['id']+'. '+result['job']['message'])
        return
    plan=request(args.port,'GET','/api/checks/'+work+'/continuation')
    print('Следующий этап: '+plan.get('stage_label','не требуется')+'.')
    if plan.get('reuse'):print('Сохраняем: '+', '.join(plan['reuse'])+'.')
    if not plan.get('available'):
        print('Причина: '+str(plan.get('reason'))+'. '+plan.get('error',''))
        if plan.get('existing_job_id'):print('Созданное продолжение: '+plan['existing_job_id'])
        return
    print('Новых запросов: '+str(plan['requests'])+'. Стоимость заранее не гарантируется.')
    if plan.get('question'):print('Уточнение: '+plan['question'])
    if args.save_form:
        form=dict(token=plan['token'],confirm_paid=False,answer='',entries=[],pages=[],condition='')
        with args.save_form.open('x') as out:json.dump(form,out,ensure_ascii=False,indent=2);out.write('\n')
        print('Шаблон сохранён: '+str(args.save_form)+'. Запросов к моделям нет.')
    else:
        print('Действие: открой работу в приложении или создай шаблон через --save-form. Запросов к моделям нет.')


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError) as error:raise SystemExit(str(error)) from None
