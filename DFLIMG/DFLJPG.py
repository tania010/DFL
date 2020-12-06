import pickle
import struct

import cv2
import numpy as np

from core.interact import interact as io
from core.structex import *
from facelib import FaceType


class DFLJPG(object):
    def __init__(self, filename):
        self.filename = filename
        self.data = b""
        self.length = 0
        self.chunks = []
        self.dfl_dict = None
        self.shape = (0,0,0)

    @staticmethod
    def load_raw(filename, loader_func=None):
        try:
            if loader_func is not None:
                data = loader_func(filename)
            else:
                with open(filename, "rb") as f:
                    data = f.read()
        except:
            raise FileNotFoundError(filename)

        try:
            inst = DFLJPG(filename)
            inst.data = data
            inst.length = len(data)
            inst_length = inst.length
            chunks = []
            data_counter = 0
            while data_counter < inst_length:
                chunk_m_l, chunk_m_h = struct.unpack ("BB", data[data_counter:data_counter+2])
                data_counter += 2

                if chunk_m_l != 0xFF:
                    raise ValueError(f"No Valid JPG info in {filename}")

                chunk_name = None
                chunk_size = None
                chunk_data = None
                chunk_ex_data = None
                is_unk_chunk = False

                if chunk_m_h & 0xF0 == 0xD0:
                    n = chunk_m_h & 0x0F

                    if n >= 0 and n <= 7:
                        chunk_name = "RST%d" % (n)
                        chunk_size = 0
                    elif n == 0x8:
                        chunk_name = "SOI"
                        chunk_size = 0
                        if len(chunks) != 0:
                            raise Exception("")
                    elif n == 0x9:
                        chunk_name = "EOI"
                        chunk_size = 0
                    elif n == 0xA:
                        chunk_name = "SOS"
                    elif n == 0xB:
                        chunk_name = "DQT"
                    elif n == 0xD:
                        chunk_name = "DRI"
                        chunk_size = 2
                    else:
                        is_unk_chunk = True
                elif chunk_m_h & 0xF0 == 0xC0:
                    n = chunk_m_h & 0x0F
                    if n == 0:
                        chunk_name = "SOF0"
                    elif n == 2:
                        chunk_name = "SOF2"
                    elif n == 4:
                        chunk_name = "DHT"
                    else:
                        is_unk_chunk = True
                elif chunk_m_h & 0xF0 == 0xE0:
                    n = chunk_m_h & 0x0F
                    chunk_name = "APP%d" % (n)
                else:
                    is_unk_chunk = True

                #if is_unk_chunk:
                #    #raise ValueError(f"Unknown chunk {chunk_m_h} in {filename}")
                #    io.log_info(f"Unknown chunk {chunk_m_h} in {filename}")

                if chunk_size == None: #variable size
                    chunk_size, = struct.unpack (">H", data[data_counter:data_counter+2])
                    chunk_size -= 2
                    data_counter += 2

                if chunk_size > 0:
                    chunk_data = data[data_counter:data_counter+chunk_size]
                    data_counter += chunk_size

                if chunk_name == "SOS":
                    c = data_counter
                    while c < inst_length and (data[c] != 0xFF or data[c+1] != 0xD9):
                        c += 1

                    chunk_ex_data = data[data_counter:c]
                    data_counter = c

                chunks.append ({'name' : chunk_name,
                                'm_h' : chunk_m_h,
                                'data' : chunk_data,
                                'ex_data' : chunk_ex_data,
                                })
            inst.chunks = chunks

            return inst
        except Exception as e:
            raise Exception (f"Corrupted JPG file {filename} {e}")

    @staticmethod
    def load(filename, loader_func=None):
        try:
            inst = DFLJPG.load_raw (filename, loader_func=loader_func)
            inst.dfl_dict = {}

            for chunk in inst.chunks:
                if chunk['name'] == 'APP0':
                    d, c = chunk['data'], 0
                    c, id, _ = struct_unpack (d, c, "=4sB")

                    if id == b"JFIF":
                        c, ver_major, ver_minor, units, Xdensity, Ydensity, Xthumbnail, Ythumbnail = struct_unpack (d, c, "=BBBHHBB")
                        #if units == 0:
                        #    inst.shape = (Ydensity, Xdensity, 3)
                    else:
                        raise Exception("Unknown jpeg ID: %s" % (id) )
                elif chunk['name'] == 'SOF0' or chunk['name'] == 'SOF2':
                    d, c = chunk['data'], 0
                    c, precision, height, width = struct_unpack (d, c, ">BHH")
                    inst.shape = (height, width, 3)

                elif chunk['name'] == 'APP15':
                    if type(chunk['data']) == bytes:
                        inst.dfl_dict = pickle.loads(chunk['data'])

            return inst
        except Exception as e:
            print (e)
            return None

    @staticmethod
    def embed_dfldict(filename, dfl_dict):
        inst = DFLJPG.load_raw (filename)
        inst.set_dict (dfl_dict)

        try:
            with open(filename, "wb") as f:
                f.write ( inst.dump() )
        except:
            raise Exception( 'cannot save %s' % (filename) )

    def has_data(self):
        return len(self.dfl_dict.keys()) != 0

    def save(self):
        try:
            with open(self.filename, "wb") as f:
                f.write ( self.dump() )
        except:
            raise Exception( f'cannot save {self.filename}' )

    def dump(self):
        data = b""

        dict_data = self.dfl_dict
        for key in list(dict_data.keys()):
            if dict_data[key] is None:
                dict_data.pop(key)

        for chunk in self.chunks:
            if chunk['name'] == 'APP15':
                self.chunks.remove(chunk)
                break

        last_app_chunk = 0
        for i, chunk in enumerate (self.chunks):
            if chunk['m_h'] & 0xF0 == 0xE0:
                last_app_chunk = i

        dflchunk = {'name' : 'APP15',
                    'm_h' : 0xEF,
                    'data' : pickle.dumps(dict_data),
                    'ex_data' : None,
                    }
        self.chunks.insert (last_app_chunk+1, dflchunk)


        for chunk in self.chunks:
            data += struct.pack ("BB", 0xFF, chunk['m_h'] )
            chunk_data = chunk['data']
            if chunk_data is not None:
                data += struct.pack (">H", len(chunk_data)+2 )
                data += chunk_data

            chunk_ex_data = chunk['ex_data']
            if chunk_ex_data is not None:
                data += chunk_ex_data

        return data

    def get_shape(self):
        return self.shape

    def get_height(self):
        for chunk in self.chunks:
            if type(chunk) == IHDR:
                return chunk.height
        return 0

    def get_dict(self):
        return self.dfl_dict

    def set_dict (self, dict_data=None):
        self.dfl_dict = dict_data

    def get_face_type(self):            return self.dfl_dict.get('face_type', FaceType.toString (FaceType.FULL) )
    def set_face_type(self, face_type): self.dfl_dict['face_type'] = face_type

    def get_landmarks(self):            return np.array ( self.dfl_dict['landmarks'] )
    def set_landmarks(self, landmarks): self.dfl_dict['landmarks'] = landmarks

    def get_eyebrows_expand_mod(self):                      return self.dfl_dict.get ('eyebrows_expand_mod', 1.0)
    def set_eyebrows_expand_mod(self, eyebrows_expand_mod): self.dfl_dict['eyebrows_expand_mod'] = eyebrows_expand_mod

    def get_source_filename(self):                  return self.dfl_dict.get ('source_filename', None)
    def set_source_filename(self, source_filename): self.dfl_dict['source_filename'] = source_filename

    def get_source_rect(self):              return self.dfl_dict.get ('source_rect', None)
    def set_source_rect(self, source_rect): self.dfl_dict['source_rect'] = source_rect

    def get_source_landmarks(self):                     return np.array ( self.dfl_dict.get('source_landmarks', None) )
    def set_source_landmarks(self, source_landmarks):   self.dfl_dict['source_landmarks'] = source_landmarks

    def get_image_to_face_mat(self):
        mat = self.dfl_dict.get ('image_to_face_mat', None)
        if mat is not None:
            return np.array (mat)
        return None
    def set_image_to_face_mat(self, image_to_face_mat):   self.dfl_dict['image_to_face_mat'] = image_to_face_mat

    def get_ie_polys(self):             return self.dfl_dict.get('ie_polys',None)
    def set_ie_polys(self, ie_polys):
        if ie_polys is not None and \
           not isinstance(ie_polys, list):
            ie_polys = ie_polys.dump()

        self.dfl_dict['ie_polys'] = ie_polys

    def get_seg_ie_polys(self): return self.dfl_dict.get('seg_ie_polys',None)
    def set_seg_ie_polys(self, seg_ie_polys):
        self.dfl_dict['seg_ie_polys'] = seg_ie_polys






