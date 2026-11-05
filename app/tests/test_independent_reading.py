"""Synthetic separation of missing student reasoning and unread source content."""
from copy import deepcopy
import unittest
from app import independent_reading as reading
from app.m05.runtime import PromptError
from app.strict_reading import response_format


def response():
    return dict(task_scope=dict(status='single_task',reason='Одна задача.'),pages=[dict(page=1,
        page_reading=dict(status='complete',reason='Весь лист просмотрен.',unread_parts=[]),
        student_work='Только ответ; обоснования нет.',
        entries=[dict(kind='math',text='x > 0',reading='clear',activity='active',author='student')])])


class IndependentReadingTests(unittest.TestCase):
    def test_student_work_description_does_not_change_image_coverage_or_literals(self):
        for note in ('Только ответ; обоснования нет.','Неполный ход решения.','Пустой лист.','Несколько строк.'):
            with self.subTest(note=note):
                value=response();value['pages'][0]['student_work']=note;original=deepcopy(value)
                projected=reading.project(value)
                self.assertEqual(projected['pages'][0]['coverage']['score_scope'],'accounted')
                self.assertEqual(projected['pages'][0]['entries'],value['pages'][0]['entries'])
                self.assertEqual(value,original)

    def test_inconsistent_complete_or_partial_reading_is_not_repaired(self):
        for status,parts in [('complete',[dict(location='Строка',impact='unknown',reason='Не прочитана.')]),('partial',[])]:
            with self.subTest(status=status):
                value=response();value['pages'][0]['page_reading'].update(status=status,unread_parts=parts)
                with self.assertRaisesRegex(PromptError,'reading_scope_inconsistent'):reading.decode(value,1)

    def test_unreadable_entry_cannot_hide_inside_complete_page(self):
        value=response();value['pages'][0]['entries'][0].update(reading='unreadable',text=None)
        with self.assertRaisesRegex(PromptError,'reading_scope_inconsistent'):reading.decode(value,1)

    def test_unreadable_and_unprocessed_pages_cannot_be_declared_nonessential(self):
        for status in ('unreadable','not_processed'):
            value=response();value['pages'][0]['page_reading']['status']=status
            self.assertEqual(reading.project(value)['pages'][0]['coverage']['score_scope'],'unresolved')

    def test_missing_or_old_coverage_fields_are_never_upgraded(self):
        for missing in ('page_reading','student_work'):
            value=response();value['pages'][0].pop(missing)
            with self.assertRaises(PromptError):reading.decode(value,1)
        value=response();value['pages'][0]['coverage']={'status':'complete'}
        with self.assertRaises(PromptError):reading.decode(value,1)

    def test_missing_and_out_of_order_pages_are_rejected(self):
        with self.assertRaises(PromptError):reading.decode(response(),2)
        value=response();value['pages'][0]['page']=2
        with self.assertRaises(PromptError):reading.decode(value,1)

    def test_all_supported_page_counts_have_strict_required_separate_fields(self):
        for count in range(1,11):
            schema=response_format(reading.schema(count))['json_schema']['schema']
            self.assertIn('task_scope',schema['required'])
            page=schema['properties']['pages']['items']
            self.assertTrue({'page_reading','student_work'}<=set(page['required']))
            self.assertNotIn('coverage',page['properties'])
